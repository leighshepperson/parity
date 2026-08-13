"""Portable dataframe schema inference and Arrow materialisation."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from parity.adapters import to_arrow
from parity.canonical import dtype_family, json_safe
from parity.models import ColumnSchema, FrameSchema, JsonValue


def portable_dtype(dtype: Any) -> str:
    """Return Parity's stable dtype vocabulary for a concrete dtype."""

    return dtype_family(dtype)


def _safe_min_max(array: pa.ChunkedArray) -> tuple[JsonValue, JsonValue]:
    family = dtype_family(array.type)
    if family not in {
        "integer",
        "float",
        "decimal",
        "date",
        "datetime",
        "time",
        "duration",
        "string",
    }:
        return None, None
    try:
        result = pc.min_max(array)
        if not result.is_valid:
            return None, None
        values = result.as_py()
        return json_safe(values.get("min")), json_safe(values.get("max"))
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, TypeError):
        return None, None


def _distinct_examples(array: pa.ChunkedArray, limit: int) -> list[JsonValue]:
    examples: list[JsonValue] = []
    seen: set[str] = set()
    for value in array.to_pylist():
        rendered = json_safe(value)
        marker = repr(rendered)
        if rendered is not None and marker not in seen:
            examples.append(rendered)
            seen.add(marker)
        if len(examples) >= limit:
            break
    return examples


def _categories(array: pa.ChunkedArray) -> list[JsonValue] | None:
    if not pa.types.is_dictionary(array.type):
        return None
    values: list[JsonValue] = []
    seen: set[str] = set()
    for chunk in array.chunks:
        for value in chunk.dictionary.to_pylist():
            rendered = json_safe(value)
            marker = repr(rendered)
            if marker not in seen:
                values.append(rendered)
                seen.add(marker)
    return values or None


def infer_schema(
    value: Any,
    *,
    min_rows: int = 0,
    max_rows: int = 30,
    example_limit: int = 5,
) -> FrameSchema:
    """Infer a portable generation schema from any registered dataframe.

    Inference preserves observed bounds and categories but intentionally emits
    portable dtype families rather than engine-specific spelling.  Arrow fields
    are nullable by default, allowing verification to probe null behaviour even
    when a small fixture happens not to contain one.
    """

    if min_rows < 0 or max_rows < min_rows:
        raise ValueError("row bounds must satisfy 0 <= min_rows <= max_rows")
    if example_limit < 0:
        raise ValueError("example_limit cannot be negative")
    table = to_arrow(value)
    columns: list[ColumnSchema] = []
    for index, field in enumerate(table.schema):
        array = table.column(index)
        minimum, maximum = _safe_min_max(array)
        non_null = len(array) - array.null_count
        unique = False
        if non_null:
            with contextlib.suppress(pa.ArrowInvalid, pa.ArrowNotImplementedError):
                unique = pc.count_distinct(array).as_py() == non_null
        timezone = field.type.tz if pa.types.is_timestamp(field.type) else None
        columns.append(
            ColumnSchema(
                name=field.name,
                dtype=portable_dtype(field.type),
                nullable=field.nullable,
                unique=unique,
                minimum=minimum,
                maximum=maximum,
                categories=_categories(array),
                examples=_distinct_examples(array, example_limit),
                timezone=timezone,
            )
        )
    if not columns:
        raise ValueError("cannot infer a schema from a dataframe with no columns")
    return FrameSchema(columns=columns, min_rows=min_rows, max_rows=max_rows)


def arrow_type(dtype: str, *, timezone: str | None = None) -> pa.DataType:
    """Map portable or common concrete dtype names to an Arrow type."""

    text = dtype.strip().lower()
    family = dtype_family(text)
    concrete: dict[str, pa.DataType] = {
        "int8": pa.int8(),
        "int16": pa.int16(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "uint8": pa.uint8(),
        "uint16": pa.uint16(),
        "uint32": pa.uint32(),
        "uint64": pa.uint64(),
        "float16": pa.float16(),
        "float32": pa.float32(),
        "float64": pa.float64(),
        "bool": pa.bool_(),
        "boolean": pa.bool_(),
        "string": pa.string(),
        "utf8": pa.string(),
        "binary": pa.binary(),
        "date32": pa.date32(),
        "date64": pa.date64(),
    }
    if text in concrete:
        return concrete[text]
    if family == "integer":
        return pa.int64()
    if family == "float":
        return pa.float64()
    if family == "boolean":
        return pa.bool_()
    if family in {"string", "category"}:
        return pa.string()
    if family == "binary":
        return pa.binary()
    if family == "date":
        return pa.date32()
    if family == "datetime":
        return pa.timestamp("us", tz=timezone)
    if family == "time":
        return pa.time64("us")
    if family == "duration":
        return pa.duration("us")
    if family == "decimal":
        return pa.decimal128(38, 9)
    if family == "list":
        return pa.list_(pa.string())
    if family == "struct":
        return pa.struct([])
    return pa.string()


def arrow_schema(schema: FrameSchema) -> pa.Schema:
    """Materialise a portable frame schema for deterministic generated cases."""

    return pa.schema(
        [
            pa.field(
                column.name,
                arrow_type(column.dtype, timezone=column.timezone),
                nullable=column.nullable,
            )
            for column in schema.columns
        ]
    )


def table_from_rows(schema: FrameSchema, rows: Iterable[dict[str, Any]]) -> pa.Table:
    """Build an Arrow table from generated rows while retaining empty dtypes."""

    materialized = list(rows)
    target = arrow_schema(schema)
    arrays = []
    for field in target:
        values = [row.get(field.name) for row in materialized]
        try:
            # ``from_pandas=False`` is semantically important: Arrow must retain
            # IEEE NaN as a value distinct from database null.
            arrays.append(pa.array(values, type=field.type, from_pandas=False))
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as error:
            raise ValueError(
                f"generated values do not conform to the declared dtype for column {field.name!r}"
            ) from error
    return pa.Table.from_arrays(arrays, names=target.names)


__all__ = [
    "arrow_schema",
    "arrow_type",
    "infer_schema",
    "portable_dtype",
    "table_from_rows",
]
