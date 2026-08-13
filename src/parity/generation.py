"""Deterministic adversarial inputs and Hypothesis dataframe strategies."""

from __future__ import annotations

import datetime as dt
import decimal
import math
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import numpy as np
import pyarrow as pa
from hypothesis import assume
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from parity.adapters import from_arrow, to_arrow
from parity.canonical import dtype_family, json_safe
from parity.models import (
    Cardinality,
    ColumnSchema,
    EqualRowCount,
    ForeignKey,
    FrameSchema,
    InputBundle,
    KeyOverlap,
    KeyRef,
    RowComparison,
)
from parity.schema import (
    arrow_type,
    infer_schema,
    key_is_unique,
    rows_satisfy_frame_constraints,
    sort_rows_for_constraints,
    table_from_rows,
    tables_from_bundle_rows,
    validate_bundle_schemas,
    validate_frame_schema,
)

AdapterName = Literal["arrow", "pandas", "polars"]
KeyNode = tuple[str, tuple[str, ...]]


def _integer_limits(dtype: str) -> tuple[int, int]:
    text = dtype.strip().lower()
    limits = {
        "int8": (-(2**7), 2**7 - 1),
        "int16": (-(2**15), 2**15 - 1),
        "int32": (-(2**31), 2**31 - 1),
        "int64": (-(2**63), 2**63 - 1),
        "uint8": (0, 2**8 - 1),
        "uint16": (0, 2**16 - 1),
        "uint32": (0, 2**32 - 1),
        "uint64": (0, 2**64 - 1),
    }
    return limits.get(text, (-(2**63), 2**63 - 1))


def _float_width(dtype: str) -> Literal[16, 32, 64]:
    text = dtype.strip().lower()
    if text == "float16":
        return 16
    if text == "float32":
        return 32
    return 64


def _float_limit(width: Literal[16, 32, 64]) -> float:
    if width == 16:
        return float(np.finfo(np.float16).max)
    if width == 32:
        return float(np.finfo(np.float32).max)
    return float(np.finfo(np.float64).max)


def _strict_float_bound(
    value: decimal.Decimal,
    width: Literal[16, 32, 64],
    *,
    increasing: bool,
) -> float:
    as_float = float(value)
    if width == 16:
        rounded = float(np.float16(as_float))
        target16 = np.float16(math.inf if increasing else -math.inf)
        adjacent = float(np.nextafter(np.float16(rounded), target16))
    elif width == 32:
        rounded = float(np.float32(as_float))
        target32 = np.float32(math.inf if increasing else -math.inf)
        adjacent = float(np.nextafter(np.float32(rounded), target32))
    else:
        rounded = as_float
        adjacent = math.nextafter(rounded, math.inf if increasing else -math.inf)
    rounded_decimal = decimal.Decimal.from_float(rounded)
    if increasing and rounded_decimal > value:
        return rounded
    if not increasing and rounded_decimal < value:
        return rounded
    return adjacent


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    """A labelled deterministic input explored before property generation."""

    name: str
    table: pa.Table

    @property
    def source(self) -> str:
        return f"adversarial:{self.name}"

    def as_adapter(self, adapter: AdapterName) -> Any:
        return from_arrow(self.table, adapter)


@dataclass(frozen=True, slots=True)
class GeneratedBundleCase:
    """A labelled atomic bundle explored before joint property generation."""

    name: str
    tables: dict[str, pa.Table]

    @property
    def source(self) -> str:
        return f"adversarial:{self.name}"

    def as_adapter(self, adapter: AdapterName) -> dict[str, Any]:
        return {name: from_arrow(table, adapter) for name, table in self.tables.items()}


def _parse_bound(value: Any, family: str) -> Any:
    if value is None:
        return None
    if family == "datetime" and isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if family == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if family == "time" and isinstance(value, str):
        return dt.time.fromisoformat(value)
    if family == "duration" and isinstance(value, (int, float)):
        return dt.timedelta(seconds=value)
    if family == "decimal" and not isinstance(value, decimal.Decimal):
        return decimal.Decimal(str(value))
    return value


def _ordinary_value(column: ColumnSchema, index: int) -> Any:
    family = dtype_family(column.dtype)
    if column.categories:
        return column.categories[index % len(column.categories)]
    if column.examples:
        return _parse_bound(column.examples[index % len(column.examples)], family)
    minimum = _parse_bound(column.minimum, family)
    maximum = _parse_bound(column.maximum, family)
    if family == "integer":
        type_low, type_high = _integer_limits(column.dtype)
        integer_low = int(minimum) if minimum is not None else max(type_low, -10)
        integer_high = int(maximum) if maximum is not None else min(type_high, 10)
        return (
            min(integer_high, integer_low + index)
            if column.unique
            else max(integer_low, min(integer_high, index))
        )
    if family == "float":
        float_low = float(minimum) if minimum is not None else -10.0
        float_high = float(maximum) if maximum is not None else 10.0
        value = float_low + index * 0.5 if column.unique else float(index) + 0.25
        return max(float_low, min(float_high, value))
    if family == "decimal":
        decimal_low = minimum if minimum is not None else decimal.Decimal("-10")
        decimal_high = maximum if maximum is not None else decimal.Decimal("10")
        return max(
            decimal_low,
            min(decimal_high, decimal.Decimal(index) + decimal.Decimal("0.25")),
        )
    if family == "boolean":
        return bool(index % 2)
    if family in {"string", "category", "object"}:
        return f"value-{index}" if column.unique else ("alpha", "βeta", "")[index % 3]
    if family == "binary":
        return f"bytes-{index}".encode()
    if family == "date":
        return dt.date(2020, 1, 1) + dt.timedelta(days=index)
    if family == "datetime":
        tz = dt.UTC if column.timezone else None
        return dt.datetime(2020, 1, 1, 12, tzinfo=tz) + dt.timedelta(days=index)
    if family == "time":
        return dt.time(index % 24, (index * 7) % 60)
    if family == "duration":
        return dt.timedelta(seconds=index - 1)
    if family == "list":
        return [str(index), "item"]
    if family == "struct":
        return {}
    return str(index)


def _base_rows(schema: FrameSchema, count: int = 3) -> list[dict[str, Any]]:
    return [
        {column.name: _ordinary_value(column, row_index) for column in schema.columns}
        for row_index in range(count)
    ]


def _extreme_value(column: ColumnSchema, high: bool) -> Any:
    family = dtype_family(column.dtype)
    if column.categories:
        return column.categories[-1 if high else 0]
    bound = column.maximum if high else column.minimum
    if bound is not None:
        return _parse_bound(bound, family)
    if family == "integer":
        return _integer_limits(column.dtype)[int(high)]
    if family == "float":
        return float("inf") if high else float("-inf")
    if family == "decimal":
        return decimal.Decimal("999999999999999999.999999999") * (1 if high else -1)
    if family in {"string", "category", "object"}:
        return "𐍈e\u0301" if high else ""
    if family == "binary":
        return b"\x00\xff" if high else b""
    if family == "date":
        return dt.date(2100, 12, 31) if high else dt.date(1900, 1, 1)
    if family == "datetime":
        tz = dt.UTC if column.timezone else None
        year = 2100 if high else 1900
        return dt.datetime(year, 12 if high else 1, 31 if high else 1, tzinfo=tz)
    if family == "duration":
        return dt.timedelta(days=36500 if high else -36500)
    return _ordinary_value(column, int(high))


def _rows_fit_contract(schema: FrameSchema, rows: list[dict[str, Any]]) -> bool:
    """Return whether a deterministic case obeys the declared input domain."""

    if not schema.min_rows <= len(rows) <= schema.max_rows:
        return False
    for column in schema.columns:
        values = [row[column.name] for row in rows]
        if not column.nullable and any(value is None for value in values):
            return False
        if column.categories is not None and any(
            value is not None and value not in column.categories for value in values
        ):
            return False
        if column.unique:
            markers = [repr(value) for value in values]
            if len(markers) != len(set(markers)):
                return False
        family = dtype_family(column.dtype)
        minimum = _parse_bound(column.minimum, family)
        maximum = _parse_bound(column.maximum, family)
        for value in values:
            if value is None:
                continue
            if (
                isinstance(value, float)
                and math.isnan(value)
                and (minimum is not None or maximum is not None)
            ):
                return False
            try:
                pa.array(
                    [value],
                    type=arrow_type(column.dtype, timezone=column.timezone),
                    from_pandas=False,
                )
            except (
                pa.ArrowInvalid,
                pa.ArrowNotImplementedError,
                pa.ArrowTypeError,
                TypeError,
                ValueError,
            ):
                return False
            try:
                if minimum is not None and value < minimum:
                    return False
                if maximum is not None and value > maximum:
                    return False
            except TypeError:
                return False
    for group in schema.unique_together:
        keys = [tuple(repr(row[name]) for name in group) for row in rows]
        if len(keys) != len(set(keys)):
            return False
    return rows_satisfy_frame_constraints(schema, rows)


def adversarial_cases(
    schema: FrameSchema | None = None,
    *,
    fixture: Any | None = None,
) -> list[GeneratedCase]:
    """Build a stable suite of boundary cases for a dataframe contract.

    Cases are returned in execution order.  Irrelevant specialised cases (for
    example NaN without a floating column) are omitted. Every synthetic case
    obeys the declared row bounds, categories, numeric bounds, and uniqueness
    constraints; an explicit fixture is always retained exactly as supplied.
    """

    if schema is None:
        if fixture is None:
            raise ValueError("adversarial generation requires a schema or fixture")
        schema = infer_schema(fixture)
    validate_frame_schema(schema)
    fixture_table = to_arrow(fixture) if fixture is not None else None
    base = _base_rows(schema)
    cases: list[GeneratedCase] = []

    def append_if_valid(name: str, rows: list[dict[str, Any]]) -> None:
        constrained_rows = sort_rows_for_constraints(schema, rows)
        if _rows_fit_contract(schema, constrained_rows):
            table = table_from_rows(schema, constrained_rows)
            if rows_satisfy_frame_constraints(schema, table.to_pylist()):
                cases.append(GeneratedCase(name, table))

    if fixture_table is not None:
        fixture_rows = fixture_table.to_pylist()
        if not rows_satisfy_frame_constraints(schema, fixture_rows):
            raise ValueError("fixture does not satisfy the declared frame constraints")
        cases.append(GeneratedCase("fixture", fixture_table))
    append_if_valid("empty", [])
    append_if_valid("singleton", base[:1])

    if any(column.nullable for column in schema.columns):
        null_row = {
            column.name: None if column.nullable else _ordinary_value(column, 0)
            for column in schema.columns
        }
        append_if_valid("nulls", [null_row, base[1]])

    float_columns = [column for column in schema.columns if dtype_family(column.dtype) == "float"]
    if float_columns:
        nan_rows = [dict(base[0]), dict(base[1])]
        for column in float_columns:
            nan_rows[0][column.name] = float("nan")
            nan_rows[1][column.name] = -0.0
        append_if_valid("nan-and-signed-zero", nan_rows)

    append_if_valid("duplicates", [base[0], dict(base[0])])

    extreme_rows = [
        {column.name: _extreme_value(column, False) for column in schema.columns},
        {column.name: _extreme_value(column, True) for column in schema.columns},
    ]
    append_if_valid("extremes", extreme_rows)

    if any(
        dtype_family(column.dtype) in {"date", "datetime", "time", "duration"}
        for column in schema.columns
    ):
        temporal = [dict(base[0]), dict(base[1])]
        for column in schema.columns:
            if dtype_family(column.dtype) in {"date", "datetime", "time", "duration"}:
                temporal[0][column.name] = _extreme_value(column, False)
                temporal[1][column.name] = _extreme_value(column, True)
        append_if_valid("temporal-boundaries", temporal)

    categorical = [column for column in schema.columns if column.categories]
    if categorical:
        category_rows = [dict(base[index]) for index in range(3)]
        for column in categorical:
            categories = column.categories
            if categories is None:  # narrowed defensively for static checkers
                continue
            for index, row in enumerate(category_rows):
                row[column.name] = categories[index % len(categories)]
        append_if_valid("categories", category_rows)

    append_if_valid("reversed-order", list(reversed(base)))
    return cases


def _key_values(table: pa.Table, ref: KeyRef) -> list[tuple[Any, ...]]:
    columns = [table.column(name).to_pylist() for name in ref.columns]
    return [tuple(column[index] for column in columns) for index in range(table.num_rows)]


def _key_marker(value: tuple[Any, ...]) -> str:
    return repr(json_safe(value))


def _non_null_key_markers(table: pa.Table, ref: KeyRef) -> set[str]:
    return {
        _key_marker(value)
        for value in _key_values(table, ref)
        if all(component is not None for component in value)
    }


def _bundle_relationships_hold(
    bundle: InputBundle,
    tables: Mapping[str, pa.Table],
) -> bool:
    """Return whether an atomic bundle satisfies all declared relationships."""

    for relationship in bundle.relationships:
        if isinstance(relationship, EqualRowCount):
            counts = {tables[name].num_rows for name in relationship.inputs}
            if len(counts) != 1:
                return False
        elif isinstance(relationship, KeyOverlap):
            shared = _non_null_key_markers(
                tables[relationship.left.input], relationship.left
            ) & _non_null_key_markers(tables[relationship.right.input], relationship.right)
            if len(shared) < relationship.min_shared:
                return False
        elif isinstance(relationship, ForeignKey):
            parent = _non_null_key_markers(tables[relationship.parent.input], relationship.parent)
            for value in _key_values(tables[relationship.child.input], relationship.child):
                if any(component is None for component in value):
                    if not relationship.allow_nulls:
                        return False
                elif _key_marker(value) not in parent:
                    return False
        elif isinstance(relationship, Cardinality):
            refs: list[KeyRef] = []
            if relationship.relationship in {"one_to_one", "one_to_many"}:
                refs.append(relationship.left)
            if relationship.relationship in {"one_to_one", "many_to_one"}:
                refs.append(relationship.right)
            for ref in refs:
                markers = [_key_marker(value) for value in _key_values(tables[ref.input], ref)]
                if len(markers) != len(set(markers)):
                    return False
    return True


def adversarial_bundle_cases(
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
    *,
    fixtures: Mapping[str, pa.Table] | None = None,
) -> list[GeneratedBundleCase]:
    """Build deterministic, relationship-valid boundary cases for an input bundle.

    A complete explicit fixture is retained exactly, matching single-frame
    behaviour. Synthetic cases pair same-named adversarial families and omit
    combinations that cannot satisfy the joint relationship contract.
    """

    validate_bundle_schemas(bundle, schemas)
    ordered_schemas = {name: schemas[name] for name in bundle.inputs}
    cases: list[GeneratedBundleCase] = []
    if fixtures is not None:
        if set(fixtures) != set(bundle.inputs):
            raise ValueError("bundle fixtures must contain every configured input")
        fixture_tables = {name: to_arrow(fixtures[name]).combine_chunks() for name in bundle.inputs}
        for name, table in fixture_tables.items():
            try:
                valid = rows_satisfy_frame_constraints(schemas[name], table.to_pylist())
            except KeyError as error:
                raise ValueError(
                    f"bundle fixture {name!r} does not match its declared frame schema"
                ) from error
            if not valid:
                raise ValueError(
                    f"bundle fixture {name!r} does not satisfy its declared frame constraints"
                )
        if not _bundle_relationships_hold(bundle, fixture_tables):
            raise ValueError("bundle fixtures do not satisfy the declared relationships")
        cases.append(
            GeneratedBundleCase(
                "fixture",
                fixture_tables,
            )
        )

    by_input = {
        name: {case.name: case.table for case in adversarial_cases(schema)}
        for name, schema in ordered_schemas.items()
    }
    first_name = next(iter(bundle.inputs))
    for case_name in by_input[first_name]:
        if not all(case_name in candidates for candidates in by_input.values()):
            continue
        tables = {name: by_input[name][case_name] for name in bundle.inputs}
        if _bundle_relationships_hold(bundle, tables):
            cases.append(GeneratedBundleCase(case_name, tables))
    return cases


def _bounded_int(column: ColumnSchema) -> SearchStrategy[Any]:
    type_minimum, type_maximum = _integer_limits(column.dtype)
    minimum = (
        max(type_minimum, int(cast(str | int | float, column.minimum)))
        if column.minimum is not None
        else type_minimum
    )
    maximum = (
        min(type_maximum, int(cast(str | int | float, column.maximum)))
        if column.maximum is not None
        else type_maximum
    )
    if minimum > maximum:
        raise ValueError(f"column {column.name!r} has no values representable by {column.dtype!r}")
    return st.integers(min_value=minimum, max_value=maximum)


def _bounded_float(column: ColumnSchema) -> SearchStrategy[Any]:
    minimum = float(cast(str | int | float, column.minimum)) if column.minimum is not None else None
    maximum = float(cast(str | int | float, column.maximum)) if column.maximum is not None else None
    return st.floats(
        min_value=minimum,
        max_value=maximum,
        allow_nan=minimum is None and maximum is None,
        allow_infinity=minimum is None and maximum is None,
        # Let Hypothesis infer whether the selected interval can contain a
        # subnormal.  Forcing True is invalid for ordinary positive ranges
        # such as 5.0..7.0 and used to abort an otherwise valid campaign.
        allow_subnormal=None,
        width=_float_width(column.dtype),
    )


def column_strategy(column: ColumnSchema, *, allow_null: bool = True) -> SearchStrategy[Any]:
    """Return a Hypothesis scalar strategy for one portable column."""

    family = dtype_family(column.dtype)
    if column.categories is not None:
        non_null_categories = [
            value
            for value in column.categories
            if value is not None and _value_fits_column(value, column)
        ]
        if not non_null_categories:
            if allow_null and column.nullable:
                return st.none()
            raise ValueError(
                f"column {column.name!r} has no non-null category representable by "
                f"dtype {column.dtype!r}"
            )
        strategy: SearchStrategy[Any] = st.sampled_from(non_null_categories)
    elif family == "integer":
        strategy = _bounded_int(column)
    elif family == "float":
        strategy = _bounded_float(column)
    elif family == "decimal":
        decimal_minimum = (
            decimal.Decimal(str(column.minimum))
            if column.minimum is not None
            else decimal.Decimal("-1e12")
        )
        decimal_maximum = (
            decimal.Decimal(str(column.maximum))
            if column.maximum is not None
            else decimal.Decimal("1e12")
        )
        strategy = st.decimals(
            min_value=decimal_minimum,
            max_value=decimal_maximum,
            places=9,
            allow_nan=False,
            allow_infinity=False,
        )
    elif family == "boolean":
        strategy = st.booleans()
    elif family in {"string", "category", "object"}:
        strategy = st.text(max_size=100)
    elif family == "binary":
        strategy = st.binary(max_size=100)
    elif family == "date":
        date_minimum = cast(dt.date | None, _parse_bound(column.minimum, family)) or dt.date(
            1900, 1, 1
        )
        date_maximum = cast(dt.date | None, _parse_bound(column.maximum, family)) or dt.date(
            2100, 12, 31
        )
        strategy = st.dates(min_value=date_minimum, max_value=date_maximum)
    elif family == "datetime":
        datetime_minimum = cast(
            dt.datetime | None, _parse_bound(column.minimum, family)
        ) or dt.datetime(1900, 1, 1)
        datetime_maximum = cast(
            dt.datetime | None, _parse_bound(column.maximum, family)
        ) or dt.datetime(2100, 12, 31)
        if column.timezone:
            strategy = st.datetimes(
                min_value=datetime_minimum.replace(tzinfo=None),
                max_value=datetime_maximum.replace(tzinfo=None),
                timezones=st.just(dt.UTC),
            )
        else:
            strategy = st.datetimes(
                min_value=datetime_minimum,
                max_value=datetime_maximum,
                timezones=st.none(),
            )
    elif family == "time":
        strategy = st.times(timezones=st.none())
    elif family == "duration":
        strategy = st.timedeltas(
            min_value=dt.timedelta(days=-36500), max_value=dt.timedelta(days=36500)
        )
    elif family == "list":
        strategy = st.lists(st.text(max_size=30), max_size=10)
    elif family == "struct":
        strategy = st.just({})
    else:
        strategy = st.text(max_size=100)
    if allow_null and column.nullable:
        strategy = st.one_of(st.none(), strategy)
    return strategy


def _domain_capacity(column: ColumnSchema) -> int | None:
    if column.categories is not None:
        return len(
            {
                repr(json_safe(value))
                for value in column.categories
                if value is not None and _value_fits_column(value, column)
            }
        ) + int(column.nullable)
    family = dtype_family(column.dtype)
    if family == "boolean":
        return 2 + int(column.nullable)
    if family == "integer":
        type_minimum, type_maximum = _integer_limits(column.dtype)
        minimum = (
            max(type_minimum, int(cast(str | int | float, column.minimum)))
            if column.minimum is not None
            else type_minimum
        )
        maximum = (
            min(type_maximum, int(cast(str | int | float, column.maximum)))
            if column.maximum is not None
            else type_maximum
        )
        return max(0, maximum - minimum + 1) + int(column.nullable)
    return None


def _effective_max_rows(schema: FrameSchema) -> int:
    maximum = schema.max_rows
    columns = {column.name: column for column in schema.columns}
    for column in schema.columns:
        capacity = _domain_capacity(column)
        if capacity == 0:
            maximum = 0
        elif column.unique and capacity is not None:
            maximum = min(maximum, capacity)
    for group in schema.unique_together:
        capacity = 1
        for name in group:
            component = _domain_capacity(columns[name])
            if component is None:
                break
            capacity *= component
        else:
            maximum = min(maximum, capacity)
    return maximum


def _value_fits_column(value: Any, column: ColumnSchema) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if column.categories is not None:
        allowed = {repr(json_safe(item)) for item in column.categories if item is not None}
        if repr(json_safe(value)) not in allowed:
            return False
    family = dtype_family(column.dtype)
    minimum = _parse_bound(column.minimum, family)
    maximum = _parse_bound(column.maximum, family)
    try:
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            return False
        pa.array(
            [value],
            type=arrow_type(column.dtype, timezone=column.timezone),
            from_pandas=False,
        )
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, TypeError, ValueError):
        return False
    return True


def _column_constraint_key(column: ColumnSchema) -> tuple[int, float]:
    if column.categories is not None:
        return 0, float(len(column.categories))
    family = dtype_family(column.dtype)
    if family == "integer":
        type_minimum, type_maximum = _integer_limits(column.dtype)
        minimum = (
            int(cast(str | int | float, column.minimum))
            if column.minimum is not None
            else type_minimum
        )
        maximum = (
            int(cast(str | int | float, column.maximum))
            if column.maximum is not None
            else type_maximum
        )
        return 1, float(maximum - minimum)
    if column.minimum is not None or column.maximum is not None:
        return 2, 0.0
    return 3, 0.0


def _shared_column_strategy(columns: list[ColumnSchema]) -> SearchStrategy[Any]:
    """Generate a non-null key component accepted by every linked schema."""

    categorized = [column for column in columns if column.categories is not None]
    if categorized:
        base = min(categorized, key=_column_constraint_key)
        candidates = [
            value
            for value in base.categories or []
            if value is not None and all(_value_fits_column(value, column) for column in columns)
        ]
        if not candidates:
            raise ValueError("linked categorical key columns have no common value")
        return st.sampled_from(candidates)
    base = min(columns, key=_column_constraint_key)
    return column_strategy(base, allow_null=False).filter(
        lambda value: all(_value_fits_column(value, column) for column in columns)
    )


def _key_node(ref: KeyRef) -> KeyNode:
    return ref.input, tuple(ref.columns)


def _bundle_key_metadata(
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
) -> tuple[
    dict[KeyNode, KeyRef],
    dict[KeyNode, set[KeyNode]],
    set[KeyNode],
    dict[KeyNode, bool],
]:
    refs: dict[KeyNode, KeyRef] = {}
    parents: dict[KeyNode, set[KeyNode]] = defaultdict(set)
    unique: set[KeyNode] = set()
    nulls_allowed: dict[KeyNode, bool] = {}

    def register(ref: KeyRef) -> KeyNode:
        node = _key_node(ref)
        refs.setdefault(node, ref)
        parents[node]
        nulls_allowed.setdefault(node, True)
        if key_is_unique(ref, schemas):
            unique.add(node)
        return node

    for relationship in bundle.relationships:
        if isinstance(relationship, KeyOverlap):
            register(relationship.left)
            register(relationship.right)
        elif isinstance(relationship, ForeignKey):
            child = register(relationship.child)
            parent = register(relationship.parent)
            if child != parent:
                parents[child].add(parent)
            nulls_allowed[child] = nulls_allowed[child] and relationship.allow_nulls
        elif isinstance(relationship, Cardinality):
            left = register(relationship.left)
            right = register(relationship.right)
            if relationship.relationship in {"one_to_one", "one_to_many"}:
                unique.add(left)
            if relationship.relationship in {"one_to_one", "many_to_one"}:
                unique.add(right)
    return refs, parents, unique, nulls_allowed


def _ancestor_nodes(node: KeyNode, parents: Mapping[KeyNode, set[KeyNode]]) -> set[KeyNode]:
    found: set[KeyNode] = set()
    pending = list(parents.get(node, ()))
    while pending:
        parent = pending.pop()
        if parent in found:
            continue
        found.add(parent)
        pending.extend(parents.get(parent, ()))
    return found


def _key_columns(
    refs: list[KeyRef],
    schemas: Mapping[str, FrameSchema],
) -> list[list[ColumnSchema]]:
    columns_by_input = {
        name: {column.name: column for column in schema.columns} for name, schema in schemas.items()
    }
    return [
        [columns_by_input[ref.input][ref.columns[position]] for ref in refs]
        for position in range(len(refs[0].columns))
    ]


def _shared_key_strategy(
    refs: list[KeyRef],
    schemas: Mapping[str, FrameSchema],
) -> SearchStrategy[tuple[Any, ...]]:
    if not refs:
        raise ValueError("a shared key strategy requires at least one key")
    return st.tuples(*(_shared_column_strategy(columns) for columns in _key_columns(refs, schemas)))


def _key_is_nullable(ref: KeyRef, schemas: Mapping[str, FrameSchema]) -> bool:
    columns = {column.name: column for column in schemas[ref.input].columns}
    return any(columns[name].nullable for name in ref.columns)


def _null_key_strategy(
    ref: KeyRef,
    schemas: Mapping[str, FrameSchema],
) -> SearchStrategy[tuple[Any, ...]]:
    columns = {column.name: column for column in schemas[ref.input].columns}
    selected = [columns[name] for name in ref.columns]
    nullable_positions = [index for index, column in enumerate(selected) if column.nullable]
    if not nullable_positions:
        raise ValueError(f"key {ref.input}.{ref.columns!r} is not nullable")

    @st.composite
    def strategy(draw: st.DrawFn) -> tuple[Any, ...]:
        null_position = draw(st.sampled_from(nullable_positions))
        return tuple(
            None if index == null_position else draw(column_strategy(column, allow_null=False))
            for index, column in enumerate(selected)
        )

    return strategy()


def _key_domain_capacity(ref: KeyRef, schemas: Mapping[str, FrameSchema]) -> int | None:
    columns = {column.name: column for column in schemas[ref.input].columns}
    capacity = 1
    for name in ref.columns:
        component = _domain_capacity(columns[name])
        if component is None:
            return None
        capacity *= component
    return capacity


def _draw_bundle_row_counts(
    draw: st.DrawFn,
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
    required_minimum: Mapping[str, int],
) -> dict[str, int]:
    parent = {name: name for name in bundle.inputs}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for relationship in bundle.relationships:
        if isinstance(relationship, EqualRowCount):
            for name in relationship.inputs[1:]:
                union(relationship.inputs[0], name)

    lower = {
        name: max(schema.min_rows, required_minimum.get(name, 0))
        for name, schema in schemas.items()
    }
    upper = {name: _effective_max_rows(schema) for name, schema in schemas.items()}
    refs, _parents, unique_nodes, _nulls_allowed = _bundle_key_metadata(bundle, schemas)
    for node in unique_nodes:
        capacity = _key_domain_capacity(refs[node], schemas)
        if capacity is not None:
            upper[refs[node].input] = min(upper[refs[node].input], capacity)
    for relationship in bundle.relationships:
        if isinstance(relationship, KeyOverlap):
            for ref in (relationship.left, relationship.right):
                lower[ref.input] = max(lower[ref.input], relationship.min_shared)

    groups: dict[str, list[str]] = defaultdict(list)
    for name in bundle.inputs:
        groups[find(name)].append(name)
    counts: dict[str, int] = {}
    for names in groups.values():
        minimum = max(lower[name] for name in names)
        maximum = min(upper[name] for name in names)
        assume(minimum <= maximum)
        count = draw(st.integers(min_value=minimum, max_value=maximum))
        counts.update(dict.fromkeys(names, count))
    return counts


def _comparison_holds(left: Any, operator: str, right: Any) -> bool:
    if operator == "lt":
        return bool(left < right)
    if operator == "le":
        return bool(left <= right)
    if operator == "eq":
        return bool(left == right)
    if operator == "ge":
        return bool(left >= right)
    return bool(left > right)


def _finite_column_values(column: ColumnSchema) -> list[Any] | None:
    family = dtype_family(column.dtype)
    if column.categories is not None:
        values = [_parse_bound(value, family) for value in column.categories if value is not None]
        return [value for value in values if _value_fits_column(value, column)]
    if family == "boolean":
        return [False, True]
    if family == "integer":
        type_minimum, type_maximum = _integer_limits(column.dtype)
        minimum = (
            max(type_minimum, int(cast(str | int | float, column.minimum)))
            if column.minimum is not None
            else type_minimum
        )
        maximum = (
            min(type_maximum, int(cast(str | int | float, column.maximum)))
            if column.maximum is not None
            else type_maximum
        )
        if maximum - minimum <= 1_000:
            return list(range(minimum, maximum + 1))
    return None


def _numeric_bounds(column: ColumnSchema) -> tuple[decimal.Decimal, decimal.Decimal]:
    family = dtype_family(column.dtype)
    if family == "integer":
        type_minimum, type_maximum = _integer_limits(column.dtype)
        minimum = (
            max(type_minimum, int(cast(str | int | float, column.minimum)))
            if column.minimum is not None
            else type_minimum
        )
        maximum = (
            min(type_maximum, int(cast(str | int | float, column.maximum)))
            if column.maximum is not None
            else type_maximum
        )
        return decimal.Decimal(minimum), decimal.Decimal(maximum)
    float_limit = (
        decimal.Decimal(str(_float_limit(_float_width(column.dtype))))
        if family == "float"
        else decimal.Decimal("1e12")
    )
    decimal_minimum = (
        decimal.Decimal(str(column.minimum)) if column.minimum is not None else -float_limit
    )
    decimal_maximum = (
        decimal.Decimal(str(column.maximum)) if column.maximum is not None else float_limit
    )
    if family == "float":
        decimal_minimum = max(-float_limit, decimal_minimum)
        decimal_maximum = min(float_limit, decimal_maximum)
    return decimal_minimum, decimal_maximum


def _numeric_value_strategy(
    column: ColumnSchema,
    *,
    minimum: decimal.Decimal | None = None,
    maximum: decimal.Decimal | None = None,
    strict_minimum: bool = False,
    strict_maximum: bool = False,
) -> SearchStrategy[Any]:
    domain_minimum, domain_maximum = _numeric_bounds(column)
    selected_minimum = max(
        domain_minimum,
        minimum if minimum is not None else domain_minimum,
    )
    selected_maximum = min(
        domain_maximum,
        maximum if maximum is not None else domain_maximum,
    )
    family = dtype_family(column.dtype)
    if family == "integer":
        integer_minimum = math.floor(selected_minimum) + int(
            strict_minimum or selected_minimum != selected_minimum.to_integral_value()
        )
        integer_maximum = math.ceil(selected_maximum) - int(
            strict_maximum or selected_maximum != selected_maximum.to_integral_value()
        )
        if integer_minimum > integer_maximum:
            return st.nothing()
        return st.integers(min_value=integer_minimum, max_value=integer_maximum)
    if family == "float":
        width = _float_width(column.dtype)
        float_minimum = float(selected_minimum)
        float_maximum = float(selected_maximum)
        if strict_minimum:
            float_minimum = _strict_float_bound(selected_minimum, width, increasing=True)
        if strict_maximum:
            float_maximum = _strict_float_bound(selected_maximum, width, increasing=False)
        if float_minimum > float_maximum:
            return st.nothing()
        return st.floats(
            min_value=float_minimum,
            max_value=float_maximum,
            allow_nan=False,
            allow_infinity=False,
            allow_subnormal=None,
            width=width,
        )
    step = decimal.Decimal("1e-9")
    if strict_minimum:
        selected_minimum += step
    if strict_maximum:
        selected_maximum -= step
    if selected_minimum > selected_maximum:
        return st.nothing()
    return st.decimals(
        min_value=selected_minimum,
        max_value=selected_maximum,
        places=9,
        allow_nan=False,
        allow_infinity=False,
    )


def _temporal_bounds(column: ColumnSchema) -> tuple[Any, Any]:
    family = dtype_family(column.dtype)
    defaults: dict[str, tuple[Any, Any]] = {
        "date": (dt.date(1900, 1, 1), dt.date(2100, 12, 31)),
        "datetime": (dt.datetime(1900, 1, 1), dt.datetime(2100, 12, 31)),
        "time": (dt.time.min, dt.time.max),
        "duration": (dt.timedelta(days=-36500), dt.timedelta(days=36500)),
    }
    minimum, maximum = defaults[family]
    if family == "datetime" and column.timezone:
        minimum = minimum.replace(tzinfo=dt.UTC)
        maximum = maximum.replace(tzinfo=dt.UTC)
    return (
        _parse_bound(column.minimum, family) if column.minimum is not None else minimum,
        _parse_bound(column.maximum, family) if column.maximum is not None else maximum,
    )


def _shift_temporal(value: Any, family: str, *, forward: bool) -> Any:
    delta = dt.timedelta(days=1) if family == "date" else dt.timedelta(microseconds=1)
    if family != "time":
        return value + delta if forward else value - delta
    anchor = dt.datetime.combine(dt.date(2000, 1, 1), value)
    shifted = anchor + delta if forward else anchor - delta
    if shifted.date() != anchor.date():
        return None
    return shifted.time()


def _temporal_value_strategy(
    column: ColumnSchema,
    *,
    minimum: Any | None = None,
    maximum: Any | None = None,
    strict_minimum: bool = False,
    strict_maximum: bool = False,
) -> SearchStrategy[Any]:
    family = dtype_family(column.dtype)
    domain_minimum, domain_maximum = _temporal_bounds(column)
    selected_minimum = max(domain_minimum, minimum if minimum is not None else domain_minimum)
    selected_maximum = min(domain_maximum, maximum if maximum is not None else domain_maximum)
    if strict_minimum:
        selected_minimum = _shift_temporal(selected_minimum, family, forward=True)
    if strict_maximum:
        selected_maximum = _shift_temporal(selected_maximum, family, forward=False)
    if selected_minimum is None or selected_maximum is None or selected_minimum > selected_maximum:
        return st.nothing()
    if family == "date":
        return st.dates(min_value=selected_minimum, max_value=selected_maximum)
    if family == "datetime":
        if column.timezone:
            return st.datetimes(
                min_value=selected_minimum.replace(tzinfo=None),
                max_value=selected_maximum.replace(tzinfo=None),
                timezones=st.just(dt.UTC),
            )
        return st.datetimes(
            min_value=selected_minimum,
            max_value=selected_maximum,
            timezones=st.none(),
        )
    if family == "time":
        return st.times(
            min_value=selected_minimum,
            max_value=selected_maximum,
            timezones=st.none(),
        )
    return st.timedeltas(min_value=selected_minimum, max_value=selected_maximum)


def _same_value_pair_strategy(
    left: ColumnSchema,
    right: ColumnSchema,
) -> SearchStrategy[tuple[Any, Any]]:
    left_family = dtype_family(left.dtype)
    right_family = dtype_family(right.dtype)
    numeric = {"integer", "float", "decimal"}
    temporal = {"date", "datetime", "time", "duration"}
    if left.categories is not None or right.categories is not None:
        categorized = left if left.categories is not None else right
        family = dtype_family(categorized.dtype)
        candidates = [
            _parse_bound(value, family)
            for value in categorized.categories or []
            if value is not None
        ]
        shared = [
            value
            for value in candidates
            if _value_fits_column(value, left) and _value_fits_column(value, right)
        ]
        if not shared:
            return st.nothing()
        return st.sampled_from(shared).map(lambda value: (value, value))
    if left_family in numeric:
        left_minimum, left_maximum = _numeric_bounds(left)
        right_minimum, right_maximum = _numeric_bounds(right)
        minimum = max(left_minimum, right_minimum)
        maximum = min(left_maximum, right_maximum)
        if left_family == right_family:
            base = (
                min((left, right), key=lambda column: _float_width(column.dtype))
                if left_family == "float"
                else left
            )
            return _numeric_value_strategy(base, minimum=minimum, maximum=maximum).map(
                lambda value: (value, value)
            )
        integer_minimum = math.ceil(minimum)
        integer_maximum = math.floor(maximum)
        if integer_minimum > integer_maximum:
            return st.nothing()

        def convert(value: int) -> tuple[Any, Any]:
            converted = {
                "integer": value,
                "float": float(value),
                "decimal": decimal.Decimal(value),
            }
            return converted[left_family], converted[right_family]

        return st.integers(min_value=integer_minimum, max_value=integer_maximum).map(convert)
    if left_family in temporal:
        left_minimum, left_maximum = _temporal_bounds(left)
        right_minimum, right_maximum = _temporal_bounds(right)
        return _temporal_value_strategy(
            left,
            minimum=max(left_minimum, right_minimum),
            maximum=min(left_maximum, right_maximum),
        ).map(lambda value: (value, value))
    return column_strategy(left, allow_null=False).map(lambda value: (value, value))


def _ordered_numeric_pair_strategy(
    left: ColumnSchema,
    right: ColumnSchema,
    *,
    strict: bool,
) -> SearchStrategy[tuple[Any, Any]]:
    _, right_maximum = _numeric_bounds(right)
    left_strategy = _numeric_value_strategy(
        left,
        maximum=right_maximum,
        strict_maximum=strict,
    )
    return left_strategy.flatmap(
        lambda left_value: _numeric_value_strategy(
            right,
            minimum=decimal.Decimal(str(left_value)),
            strict_minimum=strict,
        ).map(lambda right_value: (left_value, right_value))
    )


def _ordered_temporal_pair_strategy(
    left: ColumnSchema,
    right: ColumnSchema,
    *,
    strict: bool,
) -> SearchStrategy[tuple[Any, Any]]:
    _, right_maximum = _temporal_bounds(right)
    left_strategy = _temporal_value_strategy(
        left,
        maximum=right_maximum,
        strict_maximum=strict,
    )
    return left_strategy.flatmap(
        lambda left_value: _temporal_value_strategy(
            right,
            minimum=left_value,
            strict_minimum=strict,
        ).map(lambda right_value: (left_value, right_value))
    )


def _non_null_comparison_strategy(
    constraint: RowComparison,
    columns: Mapping[str, ColumnSchema],
) -> SearchStrategy[tuple[Any, Any]]:
    left = columns[constraint.left]
    right = columns[constraint.right]
    left_values = _finite_column_values(left)
    right_values = _finite_column_values(right)
    if left_values is not None and right_values is not None:
        pairs = [
            (left_value, right_value)
            for left_value in left_values
            for right_value in right_values
            if _comparison_holds(left_value, constraint.operator, right_value)
        ]
        return st.sampled_from(pairs) if pairs else st.nothing()
    if constraint.operator == "eq":
        return _same_value_pair_strategy(left, right)

    reversed_result = constraint.operator in {"ge", "gt"}
    ordered_left, ordered_right = (right, left) if reversed_result else (left, right)
    strict = constraint.operator in {"lt", "gt"}
    family = dtype_family(ordered_left.dtype)
    if family in {"integer", "float", "decimal"}:
        strategy = _ordered_numeric_pair_strategy(ordered_left, ordered_right, strict=strict)
    elif family in {"date", "datetime", "time", "duration"}:
        strategy = _ordered_temporal_pair_strategy(ordered_left, ordered_right, strict=strict)
    else:
        # Ordered textual domains are validated as finite categories above.
        return st.nothing()
    if reversed_result:
        return strategy.map(lambda pair: (pair[1], pair[0]))
    return strategy


def _row_comparison_pair_strategy(
    constraint: RowComparison,
    columns: Mapping[str, ColumnSchema],
) -> SearchStrategy[tuple[Any, Any]]:
    left = columns[constraint.left]
    right = columns[constraint.right]
    strategies: list[SearchStrategy[tuple[Any, Any]]] = [
        _non_null_comparison_strategy(constraint, columns)
    ]
    if left.nullable:
        strategies.append(st.tuples(st.none(), column_strategy(right)))
    if right.nullable:
        strategies.append(st.tuples(column_strategy(left, allow_null=False), st.none()))

    def survives_arrow_cast(pair: tuple[Any, Any]) -> bool:
        try:
            cast_left = pa.array(
                [pair[0]],
                type=arrow_type(left.dtype, timezone=left.timezone),
                from_pandas=False,
            )[0].as_py()
            cast_right = pa.array(
                [pair[1]],
                type=arrow_type(right.dtype, timezone=right.timezone),
                from_pandas=False,
            )[0].as_py()
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, ValueError):
            return False
        return (
            cast_left is None
            or cast_right is None
            or _comparison_holds(cast_left, constraint.operator, cast_right)
        )

    return st.one_of(*strategies).filter(survives_arrow_cast)


@st.composite
def _rows_for_count(
    draw: st.DrawFn,
    schema: FrameSchema,
    row_count: int,
) -> list[dict[str, Any]]:
    values_by_column: dict[str, list[Any]] = {}
    for column in schema.columns:
        if row_count == 0:
            values_by_column[column.name] = []
            continue
        values_by_column[column.name] = draw(
            st.lists(
                column_strategy(column),
                min_size=row_count,
                max_size=row_count,
                unique=column.unique,
            )
        )
    columns = {column.name: column for column in schema.columns}
    if row_count:
        for constraint in schema.constraints:
            if not isinstance(constraint, RowComparison):
                continue
            pairs = draw(
                st.lists(
                    _row_comparison_pair_strategy(constraint, columns),
                    min_size=row_count,
                    max_size=row_count,
                )
            )
            values_by_column[constraint.left] = [pair[0] for pair in pairs]
            values_by_column[constraint.right] = [pair[1] for pair in pairs]
    rows = [
        {column.name: values_by_column[column.name][index] for column in schema.columns}
        for index in range(row_count)
    ]
    rows = sort_rows_for_constraints(schema, rows)
    for column in schema.columns:
        if column.unique:
            markers = [repr(row[column.name]) for row in rows]
            assume(len(markers) == len(set(markers)))
    for group in schema.unique_together:
        keys = [tuple(repr(row[name]) for name in group) for row in rows]
        assume(len(keys) == len(set(keys)))
    assume(rows_satisfy_frame_constraints(schema, rows))
    return rows


def _write_key_rows(
    rows: list[dict[str, Any]],
    ref: KeyRef,
    values: list[tuple[Any, ...]],
) -> None:
    if len(rows) != len(values):
        raise ValueError("key assignment length differs from its input row count")
    for row, key in zip(rows, values, strict=True):
        for column, value in zip(ref.columns, key, strict=True):
            row[column] = value


def _distinct_keys(values: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    seen: set[str] = set()
    distinct: list[tuple[Any, ...]] = []
    for value in values:
        marker = _key_marker(value)
        if marker not in seen:
            seen.add(marker)
            distinct.append(value)
    return distinct


def _exclude_key_markers(markers: set[str]) -> Callable[[tuple[Any, ...]], bool]:
    """Build a typed Hypothesis filter that does not close over a loop variable."""

    return lambda value: _key_marker(value) not in markers


@st.composite
def _arrow_bundle_strategy(
    draw: st.DrawFn,
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
) -> dict[str, pa.Table]:
    refs, parents, unique, nulls_allowed = _bundle_key_metadata(bundle, schemas)
    overlap_requirements: dict[KeyNode, list[tuple[Any, ...]]] = defaultdict(list)
    for relationship in bundle.relationships:
        if not isinstance(relationship, KeyOverlap):
            continue
        left = _key_node(relationship.left)
        right = _key_node(relationship.right)
        constrained = {
            left,
            right,
            *_ancestor_nodes(left, parents),
            *_ancestor_nodes(right, parents),
        }
        ordered_constrained = sorted(constrained)
        shared = draw(
            st.lists(
                _shared_key_strategy([refs[node] for node in ordered_constrained], schemas),
                min_size=relationship.min_shared,
                max_size=relationship.min_shared,
                unique_by=_key_marker,
            )
        )
        for node in ordered_constrained:
            overlap_requirements[node].extend(shared)

    required_minimum: dict[str, int] = {}
    for node, values in overlap_requirements.items():
        input_name = refs[node].input
        required_minimum[input_name] = max(
            required_minimum.get(input_name, 0), len(_distinct_keys(values))
        )
    counts = _draw_bundle_row_counts(draw, bundle, schemas, required_minimum)
    rows_by_input = {
        name: draw(_rows_for_count(schema, counts[name])) for name, schema in schemas.items()
    }
    assigned: dict[KeyNode, list[tuple[Any, ...]]] = {}

    pending = list(refs)
    while pending:
        progress = False
        for node in pending.copy():
            node_parents = parents.get(node, set())
            if any(parent in pending for parent in node_parents):
                continue
            ref = refs[node]
            count = counts[ref.input]
            required = _distinct_keys(overlap_requirements.get(node, []))
            if node_parents:
                parent_key_sets = [
                    {
                        _key_marker(value): value
                        for value in assigned[parent]
                        if all(component is not None for component in value)
                    }
                    for parent in sorted(node_parents)
                ]
                common_markers = set(parent_key_sets[0]).intersection(
                    *(set(values) for values in parent_key_sets[1:])
                )
                parent_values = [parent_key_sets[0][marker] for marker in sorted(common_markers)]
            else:
                parent_values = []
            nullable = _key_is_nullable(ref, schemas) and nulls_allowed.get(node, True)

            if node_parents:
                if required:
                    parent_markers = {_key_marker(value) for value in parent_values}
                    assume(all(_key_marker(value) in parent_markers for value in required))
                choices = parent_values
                if count == 0:
                    values = []
                elif not choices:
                    assume(nullable)
                    values = draw(
                        st.lists(
                            _null_key_strategy(ref, schemas),
                            min_size=count,
                            max_size=count,
                            unique=node in unique,
                        )
                    )
                else:
                    needed = len(required)
                    assume(needed <= count)
                    remaining = count - needed
                    required_markers = {_key_marker(value) for value in required}
                    available_choices = (
                        [value for value in choices if _key_marker(value) not in required_markers]
                        if node in unique
                        else choices
                    )
                    tail_strategies: list[SearchStrategy[tuple[Any, ...]]] = []
                    if available_choices:
                        tail_strategies.append(st.sampled_from(available_choices))
                    if nullable:
                        tail_strategies.append(_null_key_strategy(ref, schemas))
                    assume(remaining == 0 or tail_strategies)
                    tail = (
                        draw(
                            st.lists(
                                st.one_of(*tail_strategies),
                                min_size=remaining,
                                max_size=remaining,
                                unique=node in unique,
                            )
                        )
                        if remaining
                        else []
                    )
                    values = [*required, *tail]
                    if node in unique:
                        assume(len({_key_marker(value) for value in values}) == len(values))
            else:
                needed = len(required)
                assume(needed <= count)
                remaining = count - needed
                if remaining:
                    try:
                        strategy = _shared_key_strategy([ref], schemas)
                    except ValueError:
                        if not nullable:
                            raise
                        strategy = _null_key_strategy(ref, schemas)
                    else:
                        if node in unique and required:
                            required_markers = {_key_marker(value) for value in required}
                            strategy = strategy.filter(_exclude_key_markers(required_markers))
                        if nullable:
                            strategy = st.one_of(strategy, _null_key_strategy(ref, schemas))
                    tail = draw(
                        st.lists(
                            strategy,
                            min_size=remaining,
                            max_size=remaining,
                            unique=node in unique,
                        )
                    )
                else:
                    tail = []
                values = [*required, *tail]
                if node in unique:
                    assume(len({_key_marker(value) for value in values}) == len(values))

            assigned[node] = values
            _write_key_rows(rows_by_input[ref.input], ref, values)
            pending.remove(node)
            progress = True
        assume(progress)

    rows_by_input = {
        name: sort_rows_for_constraints(schemas[name], rows) for name, rows in rows_by_input.items()
    }
    assume(all(_rows_fit_contract(schemas[name], rows) for name, rows in rows_by_input.items()))
    tables = tables_from_bundle_rows(schemas, rows_by_input)
    assume(
        all(
            rows_satisfy_frame_constraints(schemas[name], table.to_pylist())
            for name, table in tables.items()
        )
    )
    assume(_bundle_relationships_hold(bundle, tables))
    return tables


@st.composite
def _arrow_table_strategy(draw: st.DrawFn, schema: FrameSchema) -> pa.Table:
    maximum = _effective_max_rows(schema)
    assume(schema.min_rows <= maximum)
    row_count = draw(st.integers(min_value=schema.min_rows, max_value=maximum))
    rows = draw(_rows_for_count(schema, row_count))
    table = table_from_rows(schema, rows)
    assume(rows_satisfy_frame_constraints(schema, table.to_pylist()))
    return table


def frame_strategy(
    schema: FrameSchema,
    *,
    adapter: AdapterName = "arrow",
) -> SearchStrategy[Any]:
    """Return a shrinking, schema-aware Hypothesis dataframe strategy."""

    validate_frame_schema(schema)
    strategy: SearchStrategy[Any] = _arrow_table_strategy(schema)
    if adapter == "arrow":
        return strategy
    return strategy.map(lambda table: from_arrow(table, adapter))


def bundle_strategy(
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
    *,
    adapter: AdapterName = "arrow",
) -> SearchStrategy[Any]:
    """Return a jointly shrinking strategy for an ordered atomic input bundle."""

    validate_bundle_schemas(bundle, schemas)
    ordered_schemas = {name: schemas[name] for name in bundle.inputs}
    strategy: SearchStrategy[Any] = _arrow_bundle_strategy(bundle, ordered_schemas)
    if adapter == "arrow":
        return strategy
    return strategy.map(
        lambda tables: {name: from_arrow(table, adapter) for name, table in tables.items()}
    )


table_strategy = frame_strategy


__all__ = [
    "GeneratedBundleCase",
    "GeneratedCase",
    "adversarial_bundle_cases",
    "adversarial_cases",
    "bundle_strategy",
    "column_strategy",
    "frame_strategy",
    "table_strategy",
]
