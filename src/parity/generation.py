"""Deterministic adversarial inputs and Hypothesis dataframe strategies."""

from __future__ import annotations

import datetime as dt
import decimal
import math
from dataclasses import dataclass
from typing import Any, Literal, cast

import pyarrow as pa
from hypothesis import assume
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from parity.adapters import from_arrow, to_arrow
from parity.canonical import dtype_family
from parity.models import ColumnSchema, FrameSchema
from parity.schema import infer_schema, table_from_rows

AdapterName = Literal["arrow", "pandas", "polars"]


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
            if isinstance(value, float) and math.isnan(value):
                if minimum is not None or maximum is not None:
                    return False
                continue
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
    return True


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
    fixture_table = to_arrow(fixture) if fixture is not None else None
    base = _base_rows(schema)
    cases: list[GeneratedCase] = []

    def append_if_valid(name: str, rows: list[dict[str, Any]]) -> None:
        if _rows_fit_contract(schema, rows):
            cases.append(GeneratedCase(name, table_from_rows(schema, rows)))

    if fixture_table is not None:
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


def _bounded_int(column: ColumnSchema) -> SearchStrategy[Any]:
    type_minimum, type_maximum = _integer_limits(column.dtype)
    minimum = (
        int(cast(str | int | float, column.minimum)) if column.minimum is not None else type_minimum
    )
    maximum = (
        int(cast(str | int | float, column.maximum)) if column.maximum is not None else type_maximum
    )
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
        width=64,
    )


def column_strategy(column: ColumnSchema, *, allow_null: bool = True) -> SearchStrategy[Any]:
    """Return a Hypothesis scalar strategy for one portable column."""

    family = dtype_family(column.dtype)
    if column.categories:
        strategy: SearchStrategy[Any] = st.sampled_from(column.categories)
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
    if column.categories:
        return len(column.categories) + int(column.nullable)
    family = dtype_family(column.dtype)
    if family == "boolean":
        return 2 + int(column.nullable)
    if family == "integer" and column.minimum is not None and column.maximum is not None:
        minimum = int(cast(str | int | float, column.minimum))
        maximum = int(cast(str | int | float, column.maximum))
        return maximum - minimum + 1 + int(column.nullable)
    return None


@st.composite
def _arrow_table_strategy(draw: st.DrawFn, schema: FrameSchema) -> pa.Table:
    maximum = schema.max_rows
    for column in schema.columns:
        if column.unique and (capacity := _domain_capacity(column)) is not None:
            maximum = min(maximum, capacity)
    assume(schema.min_rows <= maximum)
    row_count = draw(st.integers(min_value=schema.min_rows, max_value=maximum))
    values_by_column: dict[str, list[Any]] = {}
    for column in schema.columns:
        strategy = column_strategy(column)
        values_by_column[column.name] = draw(
            st.lists(strategy, min_size=row_count, max_size=row_count, unique=column.unique)
        )
    rows = [
        {column.name: values_by_column[column.name][index] for column in schema.columns}
        for index in range(row_count)
    ]
    for group in schema.unique_together:
        keys = [tuple(repr(row[name]) for name in group) for row in rows]
        assume(len(keys) == len(set(keys)))
    return table_from_rows(schema, rows)


def frame_strategy(
    schema: FrameSchema,
    *,
    adapter: AdapterName = "arrow",
) -> SearchStrategy[Any]:
    """Return a shrinking, schema-aware Hypothesis dataframe strategy."""

    strategy: SearchStrategy[Any] = _arrow_table_strategy(schema)
    if adapter == "arrow":
        return strategy
    return strategy.map(lambda table: from_arrow(table, adapter))


table_strategy = frame_strategy


__all__ = [
    "GeneratedCase",
    "adversarial_cases",
    "column_strategy",
    "frame_strategy",
    "table_strategy",
]
