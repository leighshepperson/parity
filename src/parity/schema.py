"""Portable dataframe schema inference and Arrow materialisation."""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import math
import re
from collections.abc import Iterable, Mapping
from functools import cmp_to_key
from itertools import pairwise
from typing import Any, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc

from parity.adapters import to_arrow
from parity.canonical import dtype_family, json_safe
from parity.models import (
    Cardinality,
    ColumnSchema,
    EqualRowCount,
    ForeignKey,
    FrameSchema,
    InputBundle,
    JsonValue,
    KeyOverlap,
    KeyRef,
    RowComparison,
    SortedBy,
)


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


def infer_bundle_schema(
    values: Mapping[str, Any],
    *,
    min_rows: int = 0,
    max_rows: int = 30,
    example_limit: int = 5,
) -> dict[str, FrameSchema]:
    """Infer one portable schema per named frame while preserving input order."""

    if not values:
        raise ValueError("cannot infer schemas from an empty input bundle")
    return {
        name: infer_schema(
            value,
            min_rows=min_rows,
            max_rows=max_rows,
            example_limit=example_limit,
        )
        for name, value in values.items()
    }


def _schema_columns(schema: FrameSchema) -> dict[str, ColumnSchema]:
    return {column.name: column for column in schema.columns}


def _parse_constraint_bound(value: Any, family: str, timezone: str | None = None) -> Any:
    if value is None:
        return None
    if family == "datetime":
        parsed = (
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if timezone and isinstance(parsed, dt.datetime):
            zone = ZoneInfo(timezone)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=zone)
            return parsed.astimezone(zone)
        return parsed
    if family == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if family == "time" and isinstance(value, str):
        return dt.time.fromisoformat(value)
    if family == "duration" and isinstance(value, (int, float)):
        return dt.timedelta(seconds=value)
    if family == "decimal" and not isinstance(value, decimal.Decimal):
        return decimal.Decimal(str(value))
    return value


def _finite_constraint_values(column: ColumnSchema) -> list[Any] | None:
    family = dtype_family(column.dtype)
    if column.categories is not None:
        return [
            _parse_constraint_bound(value, family, column.timezone)
            for value in column.categories
            if value is not None and _value_within_column(value, column)
        ]
    if family == "boolean":
        return [False, True]
    if family == "integer":
        minimum, maximum = _integer_domain_bounds(column)
        if maximum - minimum <= 1_000:
            return list(range(minimum, maximum + 1))
    return None


def _numeric_constraint_bounds(column: ColumnSchema) -> tuple[decimal.Decimal, decimal.Decimal]:
    family = dtype_family(column.dtype)
    if family == "integer":
        minimum, maximum = _integer_domain_bounds(column)
        return decimal.Decimal(minimum), decimal.Decimal(maximum)
    decimal_minimum = (
        decimal.Decimal(str(column.minimum))
        if column.minimum is not None
        else decimal.Decimal("-Infinity")
    )
    decimal_maximum = (
        decimal.Decimal(str(column.maximum))
        if column.maximum is not None
        else decimal.Decimal("Infinity")
    )
    return decimal_minimum, decimal_maximum


def _temporal_constraint_bounds(column: ColumnSchema) -> tuple[Any, Any]:
    family = dtype_family(column.dtype)
    defaults: dict[str, tuple[Any, Any]] = {
        "date": (dt.date(1900, 1, 1), dt.date(2100, 12, 31)),
        "datetime": (dt.datetime(1900, 1, 1), dt.datetime(2100, 12, 31)),
        "time": (dt.time.min, dt.time.max),
        "duration": (dt.timedelta(days=-36500), dt.timedelta(days=36500)),
    }
    default_minimum, default_maximum = defaults[family]
    if family == "datetime" and column.timezone:
        zone = ZoneInfo(column.timezone)
        default_minimum = default_minimum.replace(tzinfo=zone)
        default_maximum = default_maximum.replace(tzinfo=zone)
    return (
        _parse_constraint_bound(column.minimum, family, column.timezone)
        if column.minimum is not None
        else default_minimum,
        _parse_constraint_bound(column.maximum, family, column.timezone)
        if column.maximum is not None
        else default_maximum,
    )


def _comparison_operator_holds(left: Any, operator: str, right: Any) -> bool:
    try:
        if operator == "lt":
            return bool(left < right)
        if operator == "le":
            return bool(left <= right)
        if operator == "eq":
            return bool(left == right)
        if operator == "ge":
            return bool(left >= right)
        return bool(left > right)
    except (TypeError, ValueError):
        return False


def _row_comparison_has_non_null_pair(
    constraint: RowComparison,
    columns: Mapping[str, ColumnSchema],
) -> bool:
    left = columns[constraint.left]
    right = columns[constraint.right]
    left_values = _finite_constraint_values(left)
    right_values = _finite_constraint_values(right)
    if left_values is not None and right_values is not None:
        return any(
            _comparison_operator_holds(left_value, constraint.operator, right_value)
            for left_value in left_values
            for right_value in right_values
        )
    if constraint.operator == "eq" and (left_values is not None or right_values is not None):
        finite = left_values if left_values is not None else right_values or []
        other = right if left_values is not None else left
        return any(_value_within_column(value, other) for value in finite)

    operator = constraint.operator
    if operator in {"ge", "gt"}:
        left, right = right, left
        operator = "le" if operator == "ge" else "lt"
    left_family = dtype_family(left.dtype)
    if left_family in {"integer", "float", "decimal"}:
        left_minimum, left_maximum = _numeric_constraint_bounds(left)
        right_minimum, right_maximum = _numeric_constraint_bounds(right)
    elif left_family in {"date", "datetime", "time", "duration"}:
        left_minimum, left_maximum = _temporal_constraint_bounds(left)
        right_minimum, right_maximum = _temporal_constraint_bounds(right)
    else:
        # Non-finite text/category domains cannot be generated constructively
        # under ordering constraints in the initial contract vocabulary.
        return operator == "eq" and left_values is None and right_values is None

    if operator == "eq":
        return max(left_minimum, right_minimum) <= min(left_maximum, right_maximum)
    if operator == "lt":
        return left_minimum < right_maximum
    return left_minimum <= right_maximum


def _finite_comparison_pairs(
    constraint: RowComparison,
    columns: Mapping[str, ColumnSchema],
) -> list[tuple[Any, Any]] | None:
    left_values = _finite_constraint_values(columns[constraint.left])
    right_values = _finite_constraint_values(columns[constraint.right])
    if left_values is None or right_values is None:
        return None
    return [
        (left, right)
        for left in left_values
        for right in right_values
        if _comparison_operator_holds(left, constraint.operator, right)
    ]


def validate_frame_schema(schema: FrameSchema) -> None:
    """Reject unsupported or provably unsatisfiable frame contracts."""

    columns = _schema_columns(schema)
    for column in schema.columns:
        if column.categories is not None:
            invalid = [
                value for value in column.categories if not _value_within_column(value, column)
            ]
            valid = [value for value in column.categories if _value_within_column(value, column)]
            if invalid and not valid:
                raise ValueError(
                    f"column {column.name!r} has no values representable in its categorical "
                    "dtype, bounds, regex and length constraints"
                )
            if invalid:
                raise ValueError(
                    f"column {column.name!r} contains categorical values outside its dtype, "
                    "bounds, regex or length constraints"
                )
        invalid_examples = [
            value for value in column.examples if not _value_within_column(value, column)
        ]
        if invalid_examples:
            raise ValueError(
                f"column {column.name!r} contains examples outside its declared domain"
            )
        if dtype_family(column.dtype) == "integer":
            minimum, maximum = _integer_domain_bounds(column)
            if minimum > maximum and not column.nullable:
                raise ValueError(
                    f"column {column.name!r} has no values representable by {column.dtype!r}"
                )

    compared_columns: set[str] = set()
    for constraint in schema.constraints:
        if not isinstance(constraint, RowComparison):
            continue
        selected = {constraint.left, constraint.right}
        overlapping = selected & compared_columns
        if overlapping:
            raise ValueError(
                "overlapping row_comparison constraints are not supported yet; "
                f"columns already constrained: {sorted(overlapping)}"
            )
        compared_columns.update(selected)
        left = columns[constraint.left]
        right = columns[constraint.right]
        left_family = dtype_family(left.dtype)
        right_family = dtype_family(right.dtype)
        if (
            constraint.operator == "eq"
            and left_family != right_family
            and {left_family, right_family} <= {"integer", "float", "decimal"}
            and left.categories is None
            and right.categories is None
        ):
            left_minimum, left_maximum = _numeric_constraint_bounds(left)
            right_minimum, right_maximum = _numeric_constraint_bounds(right)
            common_minimum = max(left_minimum, right_minimum)
            common_maximum = min(left_maximum, right_maximum)
            no_integral_value = (
                common_minimum.is_finite()
                and common_maximum.is_finite()
                and math.ceil(common_minimum) > math.floor(common_maximum)
            )
            if no_integral_value and not (left.nullable or right.nullable):
                raise ValueError(
                    "cross-family numeric equality requires a shared integral value "
                    "or a nullable comparison column"
                )
        if (
            constraint.operator != "eq"
            and {left_family, right_family} <= {"string", "category"}
            and (left.categories is None or right.categories is None)
        ):
            raise ValueError(
                "ordered row_comparison text domains require categories on both columns"
            )
        if not _row_comparison_has_non_null_pair(constraint, columns) and not (
            left.nullable or right.nullable
        ):
            raise ValueError(
                f"row_comparison {constraint.left} {constraint.operator} "
                f"{constraint.right} has no satisfying values in the declared domains"
            )
        finite_pairs = _finite_comparison_pairs(constraint, columns)
        if finite_pairs is None or schema.min_rows == 0:
            continue
        left_values = _finite_constraint_values(left) or []
        right_values = _finite_constraint_values(right) or []
        if left.unique:
            left_capacity = (
                len({repr(value) for value in left_values})
                if right.nullable
                else len({repr(pair[0]) for pair in finite_pairs})
            ) + int(left.nullable)
            if schema.min_rows > left_capacity:
                raise ValueError(
                    f"row_comparison leaves only {left_capacity} distinct values for unique "
                    f"column {left.name!r}, below min_rows={schema.min_rows}"
                )
        if right.unique:
            right_capacity = (
                len({repr(value) for value in right_values})
                if left.nullable
                else len({repr(pair[1]) for pair in finite_pairs})
            ) + int(right.nullable)
            if schema.min_rows > right_capacity:
                raise ValueError(
                    f"row_comparison leaves only {right_capacity} distinct values for unique "
                    f"column {right.name!r}, below min_rows={schema.min_rows}"
                )
        constrained_names = {constraint.left, constraint.right}
        for group in schema.unique_together:
            if set(group) != constrained_names:
                continue
            pair_capacity = len({(repr(left), repr(right)) for left, right in finite_pairs})
            if left.nullable:
                pair_capacity += len(right_values)
            if right.nullable:
                pair_capacity += len(left_values)
            if left.nullable and right.nullable:
                pair_capacity += 1
            if schema.min_rows > pair_capacity:
                raise ValueError(
                    "row_comparison leaves too few distinct pairs for unique_together "
                    f"{group!r} and min_rows={schema.min_rows}"
                )


def _ordered_value_compare(left: Any, right: Any, constraint: SortedBy) -> int:
    left_null = left is None
    right_null = right is None
    if left_null or right_null:
        if left_null and right_null:
            return 0
        null_order = -1 if constraint.nulls == "first" else 1
        return null_order if left_null else -null_order

    left_nan = isinstance(left, float) and math.isnan(left)
    right_nan = isinstance(right, float) and math.isnan(right)
    if left_nan or right_nan:
        if left_nan and right_nan:
            return 0
        result = 1 if left_nan else -1
    else:
        try:
            result = (left > right) - (left < right)
        except (TypeError, ValueError) as error:
            raise ValueError("sorted_by values are not mutually orderable") from error
    return -result if constraint.descending else result


def _row_order_compare(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    constraint: SortedBy,
) -> int:
    for name in constraint.columns:
        result = _ordered_value_compare(left[name], right[name], constraint)
        if result:
            return result
    return 0


def sort_rows_for_constraints(
    schema: FrameSchema,
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return rows in the schema's declared stable lexicographic order."""

    materialized = [dict(row) for row in rows]
    constraint = next(
        (item for item in schema.constraints if isinstance(item, SortedBy)),
        None,
    )
    if constraint is None:
        return materialized

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        return _row_order_compare(left, right, constraint)

    return sorted(materialized, key=cmp_to_key(compare))


def rows_satisfy_frame_constraints(
    schema: FrameSchema,
    rows: Iterable[Mapping[str, Any]],
) -> bool:
    """Return whether rows satisfy every declared valid-domain constraint."""

    materialized = list(rows)
    for constraint in schema.constraints:
        if isinstance(constraint, SortedBy):
            if any(
                _row_order_compare(left, right, constraint) > 0
                for left, right in pairwise(materialized)
            ):
                return False
            continue
        for row in materialized:
            left = row[constraint.left]
            right = row[constraint.right]
            if left is None or right is None:
                continue
            if not _comparison_operator_holds(left, constraint.operator, right):
                return False
    return True


def _relationship_refs(relationship: object) -> tuple[KeyRef, KeyRef] | None:
    if isinstance(relationship, KeyOverlap | Cardinality):
        return relationship.left, relationship.right
    if isinstance(relationship, ForeignKey):
        return relationship.child, relationship.parent
    return None


def _non_null_capacity(column: ColumnSchema) -> int | None:
    if column.categories is not None:
        return len(
            {
                repr(value)
                for value in column.categories
                if value is not None and _value_within_column(value, column)
            }
        )
    family = dtype_family(column.dtype)
    if family == "boolean":
        return 2
    if family == "integer":
        minimum, maximum = _integer_domain_bounds(column)
        return max(0, maximum - minimum + 1)
    return None


def _total_column_capacity(column: ColumnSchema) -> int | None:
    capacity = _non_null_capacity(column)
    return None if capacity is None else capacity + int(column.nullable)


def key_capacity(ref: KeyRef, schemas: Mapping[str, FrameSchema]) -> int | None:
    """Return the finite non-null tuple capacity for a resolved key, when known."""

    columns = _schema_columns(schemas[ref.input])
    capacity = 1
    for name in ref.columns:
        component = _non_null_capacity(columns[name])
        if component is None:
            return None
        capacity *= component
    return capacity


def _key_total_capacity(ref: KeyRef, schemas: Mapping[str, FrameSchema]) -> int | None:
    """Return finite key-tuple capacity including nullable component values."""

    columns = _schema_columns(schemas[ref.input])
    capacity = 1
    for name in ref.columns:
        component = _total_column_capacity(columns[name])
        if component is None:
            return None
        capacity *= component
    return capacity


def _key_null_capacity(ref: KeyRef, schemas: Mapping[str, FrameSchema]) -> int | None:
    """Return finite capacity of tuples containing at least one null component."""

    total = _key_total_capacity(ref, schemas)
    non_null = key_capacity(ref, schemas)
    if total is None or non_null is None:
        return None
    return total - non_null


def key_is_unique(ref: KeyRef, schemas: Mapping[str, FrameSchema]) -> bool:
    """Return whether a frame schema already guarantees uniqueness for a key."""

    schema = schemas[ref.input]
    columns = _schema_columns(schema)
    selected = set(ref.columns)
    if any(columns[name].unique for name in ref.columns):
        return True
    return any(set(group) <= selected for group in schema.unique_together)


def _validate_key_pair(
    left: KeyRef,
    right: KeyRef,
    schemas: Mapping[str, FrameSchema],
) -> None:
    if len(left.columns) != len(right.columns):
        raise ValueError("paired relationship keys must contain the same number of columns")
    left_columns = _schema_columns(schemas[left.input])
    right_columns = _schema_columns(schemas[right.input])
    for left_name, right_name in zip(left.columns, right.columns, strict=True):
        left_column = left_columns[left_name]
        right_column = right_columns[right_name]
        left_family = dtype_family(left_column.dtype)
        right_family = dtype_family(right_column.dtype)
        if left_family != right_family:
            raise ValueError(
                "paired key columns must have matching portable dtype families: "
                f"{left.input}.{left_name} is {left_family}, "
                f"{right.input}.{right_name} is {right_family}"
            )
        if left_family == "datetime" and left_column.timezone != right_column.timezone:
            raise ValueError(
                "paired datetime key columns must have the same timezone: "
                f"{left.input}.{left_name} and {right.input}.{right_name}"
            )


def _column_domain_intersects(left: ColumnSchema, right: ColumnSchema) -> bool:
    """Return whether two compatible columns share a possible non-null value."""

    capacity = _column_intersection_capacity(left, right)
    if capacity is not None:
        return capacity > 0
    if dtype_family(left.dtype) in {"float", "decimal"}:
        left_low = (
            float(cast(str | int | float, left.minimum))
            if left.minimum is not None
            else float("-inf")
        )
        left_high = (
            float(cast(str | int | float, left.maximum))
            if left.maximum is not None
            else float("inf")
        )
        right_low = (
            float(cast(str | int | float, right.minimum))
            if right.minimum is not None
            else float("-inf")
        )
        right_high = (
            float(cast(str | int | float, right.maximum))
            if right.maximum is not None
            else float("inf")
        )
        return max(left_low, right_low) <= min(left_high, right_high)
    return True


def _value_within_column(value: Any, column: ColumnSchema) -> bool:
    if value is None:
        return column.nullable
    family = dtype_family(column.dtype)
    try:
        comparable_value = _parse_constraint_bound(value, family, column.timezone)
        if column.categories is not None and repr(comparable_value) not in {
            repr(_parse_constraint_bound(candidate, family, column.timezone))
            for candidate in column.categories
            if candidate is not None
        }:
            return False
        if family in {"string", "category"}:
            if not isinstance(comparable_value, str):
                return False
            if column.min_length is not None and len(comparable_value) < column.min_length:
                return False
            if column.max_length is not None and len(comparable_value) > column.max_length:
                return False
            if column.regex is not None and re.fullmatch(column.regex, comparable_value) is None:
                return False
        pa.array(
            [comparable_value],
            type=arrow_type(column.dtype, timezone=column.timezone),
            from_pandas=False,
        )
        minimum = _parse_constraint_bound(column.minimum, family, column.timezone)
        maximum = _parse_constraint_bound(column.maximum, family, column.timezone)
        if minimum is not None and comparable_value < minimum:
            return False
        if maximum is not None and comparable_value > maximum:
            return False
    except (
        pa.ArrowInvalid,
        pa.ArrowNotImplementedError,
        pa.ArrowTypeError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _integer_domain_bounds(column: ColumnSchema) -> tuple[int, int]:
    concrete_limits = {
        "int8": (-(2**7), 2**7 - 1),
        "int16": (-(2**15), 2**15 - 1),
        "int32": (-(2**31), 2**31 - 1),
        "int64": (-(2**63), 2**63 - 1),
        "uint8": (0, 2**8 - 1),
        "uint16": (0, 2**16 - 1),
        "uint32": (0, 2**32 - 1),
        "uint64": (0, 2**64 - 1),
    }
    type_minimum, type_maximum = concrete_limits.get(
        column.dtype.strip().lower(), (-(2**63), 2**63 - 1)
    )
    minimum = (
        int(cast(str | int | float, column.minimum)) if column.minimum is not None else type_minimum
    )
    maximum = (
        int(cast(str | int | float, column.maximum)) if column.maximum is not None else type_maximum
    )
    return max(type_minimum, minimum), min(type_maximum, maximum)


def _column_intersection_capacity(left: ColumnSchema, right: ColumnSchema) -> int | None:
    """Return finite shared non-null capacity, or ``None`` when unbounded."""

    if left.categories is not None or right.categories is not None:
        categorized = left if left.categories is not None else right
        return len(
            {
                repr(value)
                for value in categorized.categories or []
                if value is not None
                and _value_within_column(value, left)
                and _value_within_column(value, right)
            }
        )
    family = dtype_family(left.dtype)
    if family == "boolean":
        return 2
    if family == "integer":
        left_minimum, left_maximum = _integer_domain_bounds(left)
        right_minimum, right_maximum = _integer_domain_bounds(right)
        return max(
            0,
            min(left_maximum, right_maximum) - max(left_minimum, right_minimum) + 1,
        )
    return None


def _key_intersection_capacity(
    left: KeyRef,
    right: KeyRef,
    schemas: Mapping[str, FrameSchema],
) -> int | None:
    left_columns = _schema_columns(schemas[left.input])
    right_columns = _schema_columns(schemas[right.input])
    capacity = 1
    for left_name, right_name in zip(left.columns, right.columns, strict=True):
        component = _column_intersection_capacity(
            left_columns[left_name], right_columns[right_name]
        )
        if component is None:
            return None
        capacity *= component
    return capacity


def _refs_intersection_capacity(
    refs: Iterable[KeyRef],
    schemas: Mapping[str, FrameSchema],
) -> int | None:
    """Return shared non-null capacity across positionally compatible keys."""

    selected = list(refs)
    if not selected:
        return None
    columns_by_input = {name: _schema_columns(schema) for name, schema in schemas.items()}
    capacity = 1
    for position in range(len(selected[0].columns)):
        columns = [columns_by_input[ref.input][ref.columns[position]] for ref in selected]
        categorized = [column for column in columns if column.categories is not None]
        if categorized:
            base = min(categorized, key=lambda column: len(column.categories or []))
            component = len(
                {
                    repr(value)
                    for value in base.categories or []
                    if value is not None
                    and all(_value_within_column(value, column) for column in columns)
                }
            )
        elif dtype_family(columns[0].dtype) == "boolean":
            component = 2
        elif dtype_family(columns[0].dtype) == "integer":
            bounds = [_integer_domain_bounds(column) for column in columns]
            component = max(
                0,
                min(maximum for _, maximum in bounds) - max(minimum for minimum, _ in bounds) + 1,
            )
        else:
            return None
        capacity *= component
    return capacity


def _schema_unique_capacity(schema: FrameSchema) -> int | None:
    """Return the tightest finite row capacity implied by schema uniqueness."""

    columns = _schema_columns(schema)
    capacities: list[int] = []
    for column in schema.columns:
        if column.unique and (capacity := _total_column_capacity(column)) is not None:
            capacities.append(capacity)
    for group in schema.unique_together:
        capacity = 1
        for name in group:
            component = _total_column_capacity(columns[name])
            if component is None:
                break
            capacity *= component
        else:
            capacities.append(capacity)
    return min(capacities) if capacities else None


def _key_domains_intersect(
    left: KeyRef,
    right: KeyRef,
    schemas: Mapping[str, FrameSchema],
) -> bool:
    left_columns = _schema_columns(schemas[left.input])
    right_columns = _schema_columns(schemas[right.input])
    return all(
        _column_domain_intersects(left_columns[left_name], right_columns[right_name])
        for left_name, right_name in zip(left.columns, right.columns, strict=True)
    )


def validate_bundle_schemas(
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
) -> None:
    """Validate resolved schemas against every bundle relationship.

    Fixture-only inputs cannot be checked fully while parsing TOML.  Engines call
    this function after inferring their schemas, before deterministic or generated
    examples are constructed.
    """

    expected = set(bundle.inputs)
    supplied = set(schemas)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise ValueError(
            f"resolved bundle schemas differ from inputs; missing={missing}, extra={extra}"
        )

    for schema in schemas.values():
        validate_frame_schema(schema)

    columns_by_input = {name: _schema_columns(schema) for name, schema in schemas.items()}
    schema_row_capacities: dict[str, int] = {}
    for name, schema in schemas.items():
        empty_columns = [column for column in schema.columns if _total_column_capacity(column) == 0]
        if empty_columns:
            schema_row_capacities[name] = 0
            if schema.min_rows > 0:
                column = empty_columns[0]
                raise ValueError(
                    f"input {name!r} column {column.name!r} has no values representable "
                    f"by dtype {column.dtype!r}"
                )
            continue
        capacity = _schema_unique_capacity(schema)
        if capacity is not None:
            schema_row_capacities[name] = min(schema.max_rows, capacity)
            if capacity < schema.min_rows:
                raise ValueError(
                    f"input {name!r} requires min_rows={schema.min_rows}, but its finite "
                    "unique domain is smaller"
                )
        else:
            schema_row_capacities[name] = schema.max_rows

    unique_refs: list[KeyRef] = []
    key_refs: dict[tuple[str, tuple[str, ...]], KeyRef] = {}
    foreign_parents: dict[tuple[str, tuple[str, ...]], set[tuple[str, tuple[str, ...]]]] = {}
    foreign_nulls_allowed: dict[tuple[str, tuple[str, ...]], bool] = {}

    def key_node(ref: KeyRef) -> tuple[str, tuple[str, ...]]:
        node = (ref.input, tuple(ref.columns))
        key_refs.setdefault(node, ref)
        foreign_parents.setdefault(node, set())
        return node

    for relationship in bundle.relationships:
        refs = _relationship_refs(relationship)
        if refs is not None:
            for ref in refs:
                unknown = set(ref.columns) - columns_by_input[ref.input].keys()
                if unknown:
                    raise ValueError(
                        f"key for input {ref.input!r} references unknown columns: {sorted(unknown)}"
                    )
                key_node(ref)
            _validate_key_pair(*refs, schemas)

        if isinstance(relationship, EqualRowCount):
            minimum = max(schemas[name].min_rows for name in relationship.inputs)
            maximum = min(schema_row_capacities[name] for name in relationship.inputs)
            if minimum > maximum:
                raise ValueError(
                    f"equal_row_count inputs have incompatible row ranges: {relationship.inputs}"
                )
        elif isinstance(relationship, KeyOverlap):
            if not _key_domains_intersect(relationship.left, relationship.right, schemas):
                raise ValueError("key_overlap key domains have no common non-null value")
            shared_capacity = _key_intersection_capacity(
                relationship.left, relationship.right, schemas
            )
            if shared_capacity is not None and relationship.min_shared > shared_capacity:
                raise ValueError(
                    f"key_overlap min_shared={relationship.min_shared} exceeds the "
                    "shared non-null key domain"
                )
            for ref in (relationship.left, relationship.right):
                if relationship.min_shared > schema_row_capacities[ref.input]:
                    raise ValueError(
                        f"key_overlap min_shared={relationship.min_shared} exceeds "
                        f"{ref.input!r} max_rows"
                    )
                capacity = key_capacity(ref, schemas)
                if capacity is not None and relationship.min_shared > capacity:
                    raise ValueError(
                        f"key_overlap min_shared={relationship.min_shared} exceeds the "
                        f"non-null domain of {ref.input!r}"
                    )
        elif isinstance(relationship, ForeignKey):
            child_node = key_node(relationship.child)
            parent_node = key_node(relationship.parent)
            if child_node != parent_node:
                foreign_parents[child_node].add(parent_node)
            foreign_nulls_allowed[child_node] = foreign_nulls_allowed.get(child_node, True) and (
                relationship.allow_nulls
            )
            child = schemas[relationship.child.input]
            child_columns = columns_by_input[relationship.child.input]
            nullable_key = any(child_columns[name].nullable for name in relationship.child.columns)
            domains_intersect = _key_domains_intersect(
                relationship.child, relationship.parent, schemas
            )
            if (
                child.min_rows > 0
                and not domains_intersect
                and not (relationship.allow_nulls and nullable_key)
            ):
                raise ValueError("foreign_key child and parent domains do not overlap")
            if (
                schema_row_capacities[relationship.parent.input] == 0
                and child.min_rows > 0
                and (not relationship.allow_nulls or not nullable_key)
            ):
                raise ValueError(
                    f"foreign_key parent {relationship.parent.input!r} cannot be empty "
                    f"when child {relationship.child.input!r} requires non-null rows"
                )
        elif isinstance(relationship, Cardinality):
            if relationship.relationship in {"one_to_one", "one_to_many"}:
                unique_refs.append(relationship.left)
            if relationship.relationship in {"one_to_one", "many_to_one"}:
                unique_refs.append(relationship.right)

    nodes_by_input: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for node in key_refs:
        nodes_by_input.setdefault(node[0], []).append(node)
    for input_name, nodes in nodes_by_input.items():
        for index, left_node in enumerate(nodes):
            left_columns = set(left_node[1])
            for right_node in nodes[index + 1 :]:
                if left_columns & set(right_node[1]):
                    raise ValueError(
                        "overlapping non-identical key references are not supported for "
                        f"input {input_name!r}: {list(left_node[1])!r} and "
                        f"{list(right_node[1])!r}"
                    )

    def ancestors(node: tuple[str, tuple[str, ...]]) -> set[tuple[str, tuple[str, ...]]]:
        found: set[tuple[str, tuple[str, ...]]] = set()
        pending = list(foreign_parents.get(node, ()))
        while pending:
            parent = pending.pop()
            if parent in found:
                continue
            found.add(parent)
            pending.extend(foreign_parents.get(parent, ()))
        return found

    if any(node in ancestors(node) for node in foreign_parents):
        raise ValueError("foreign_key relationships cannot contain a cycle")

    for node, direct_parents in foreign_parents.items():
        if not direct_parents:
            continue
        ref = key_refs[node]
        constrained = [ref, *(key_refs[parent] for parent in ancestors(node))]
        capacity = _refs_intersection_capacity(constrained, schemas)
        columns = columns_by_input[ref.input]
        nullable = any(columns[name].nullable for name in ref.columns)
        if (
            capacity == 0
            and schemas[ref.input].min_rows > 0
            and not (foreign_nulls_allowed.get(node, True) and nullable)
        ):
            raise ValueError(
                f"foreign_key child {ref.input!r} has no value shared by all parent domains"
            )

    # An overlap value on a foreign-key child must also fit every transitive
    # parent. Validate that combined domain rather than checking edges in
    # isolation, which would miss A⊆B⊆C intersections that are empty overall.
    overlaps_by_node: dict[
        tuple[str, tuple[str, ...]],
        list[tuple[KeyOverlap, set[tuple[str, tuple[str, ...]]]]],
    ] = {}
    for relationship in bundle.relationships:
        if not isinstance(relationship, KeyOverlap):
            continue
        left_node = key_node(relationship.left)
        right_node = key_node(relationship.right)
        constrained_nodes = {
            left_node,
            right_node,
            *ancestors(left_node),
            *ancestors(right_node),
        }
        for node in (left_node, right_node):
            overlaps_by_node.setdefault(node, []).append((relationship, constrained_nodes))
        for node in constrained_nodes:
            ref = key_refs[node]
            if relationship.min_shared > schema_row_capacities[ref.input]:
                raise ValueError(
                    f"key_overlap min_shared={relationship.min_shared} exceeds the row "
                    f"capacity of transitive foreign-key input {ref.input!r}"
                )
        capacity = _refs_intersection_capacity(
            (key_refs[node] for node in constrained_nodes), schemas
        )
        if capacity is not None and capacity < relationship.min_shared:
            raise ValueError(
                f"key_overlap min_shared={relationship.min_shared} cannot satisfy its "
                "transitive foreign-key domains"
            )

    # Distinct overlap edges may reuse only values admitted by every edge. A
    # shared endpoint needs enough rows for the remaining per-edge values.
    for node, overlaps in overlaps_by_node.items():
        for index, (left_overlap, left_nodes) in enumerate(overlaps):
            for right_overlap, right_nodes in overlaps[index + 1 :]:
                common_capacity = _refs_intersection_capacity(
                    (key_refs[item] for item in left_nodes | right_nodes), schemas
                )
                if common_capacity is None:
                    continue
                maximum_reuse = min(
                    left_overlap.min_shared,
                    right_overlap.min_shared,
                    common_capacity,
                )
                required_rows = left_overlap.min_shared + right_overlap.min_shared - maximum_reuse
                if required_rows > schema_row_capacities[key_refs[node].input]:
                    raise ValueError(
                        "key_overlap requirements need more distinct keys than the row "
                        f"capacity of input {key_refs[node].input!r}"
                    )

    for ref in unique_refs:
        capacity = _key_total_capacity(ref, schemas)
        if capacity is not None and capacity < schemas[ref.input].min_rows:
            raise ValueError(
                f"cardinality requires a unique key for {ref.input!r}, but its domain "
                f"cannot satisfy min_rows={schemas[ref.input].min_rows}"
            )

    relation_unique_capacities: dict[str, list[int]] = {}
    for ref in unique_refs:
        capacity = _key_total_capacity(ref, schemas)
        if capacity is not None:
            relation_unique_capacities.setdefault(ref.input, []).append(capacity)
    for relationship in bundle.relationships:
        if not isinstance(relationship, EqualRowCount):
            continue
        minimum = max(schemas[name].min_rows for name in relationship.inputs)
        maximum = min(
            schema_row_capacities[name]
            if name not in relation_unique_capacities
            else min(schema_row_capacities[name], *relation_unique_capacities[name])
            for name in relationship.inputs
        )
        if minimum > maximum:
            raise ValueError(
                f"equal_row_count inputs have incompatible row ranges after cardinality: "
                f"{relationship.inputs}"
            )

    unique_markers = {(ref.input, tuple(ref.columns)) for ref in unique_refs}
    unique_nodes = {
        node
        for node, ref in key_refs.items()
        if node in unique_markers or key_is_unique(ref, schemas)
    }
    for node in unique_nodes:
        node_parents = ancestors(node)
        if not node_parents:
            continue
        ref = key_refs[node]
        constrained = [ref, *(key_refs[parent] for parent in node_parents)]
        domain_capacity = _refs_intersection_capacity(constrained, schemas)
        parent_row_capacity = min(
            schema_row_capacities[key_refs[parent].input] for parent in node_parents
        )
        non_null_capacity = (
            parent_row_capacity
            if domain_capacity is None
            else min(domain_capacity, parent_row_capacity)
        )
        columns = columns_by_input[ref.input]
        nullable = any(columns[name].nullable for name in ref.columns)
        if foreign_nulls_allowed.get(node, True) and nullable:
            null_capacity = _key_null_capacity(ref, schemas)
            if null_capacity is None:
                continue
            non_null_capacity += null_capacity
        if schemas[ref.input].min_rows > non_null_capacity:
            raise ValueError(
                f"unique foreign_key child {ref.input!r} exceeds the combined parent domain"
            )

    for relationship in bundle.relationships:
        if not isinstance(relationship, ForeignKey):
            continue
        child_marker = (relationship.child.input, tuple(relationship.child.columns))
        child_unique = child_marker in unique_markers or key_is_unique(relationship.child, schemas)
        child_columns = columns_by_input[relationship.child.input]
        nullable_key = any(child_columns[name].nullable for name in relationship.child.columns)
        if child_unique:
            maximum_distinct = schema_row_capacities[relationship.parent.input]
            if relationship.allow_nulls and nullable_key:
                null_capacity = _key_null_capacity(relationship.child, schemas)
                if null_capacity is None:
                    continue
                maximum_distinct += null_capacity
            if schemas[relationship.child.input].min_rows > maximum_distinct:
                raise ValueError(
                    "a unique foreign-key child cannot require more rows than its parent "
                    "and nullable key domains permit"
                )


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


def _arrow_materialization_value(value: Any, target: pa.DataType) -> Any:
    """Project aware datetimes to the instant Arrow timestamps actually store.

    Arrow's timezone timestamp is an epoch value annotated with a zone name;
    it has no separate PEP 495 ``fold`` field.  Supplying UTC makes ambiguous
    local instants portable even when an older PyArrow delegates Python
    materialisation to pytz, whose datetime objects always expose ``fold=0``.
    """

    if (
        isinstance(value, dt.datetime)
        and pa.types.is_timestamp(target)
        and target.tz is not None
        and value.tzinfo is not None
        and value.utcoffset() is not None
    ):
        return value.astimezone(dt.UTC)
    return value


def table_from_rows(schema: FrameSchema, rows: Iterable[dict[str, Any]]) -> pa.Table:
    """Build an Arrow table from generated rows while retaining empty dtypes."""

    materialized = list(rows)
    target = arrow_schema(schema)
    arrays = []
    for field in target:
        values = [
            _arrow_materialization_value(row.get(field.name), field.type) for row in materialized
        ]
        try:
            # ``from_pandas=False`` is semantically important: Arrow must retain
            # IEEE NaN as a value distinct from database null.
            arrays.append(pa.array(values, type=field.type, from_pandas=False))
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as error:
            raise ValueError(
                f"generated values do not conform to the declared dtype for column {field.name!r}"
            ) from error
    return pa.Table.from_arrays(arrays, names=target.names)


def tables_from_bundle_rows(
    schemas: Mapping[str, FrameSchema],
    rows_by_input: Mapping[str, Iterable[dict[str, Any]]],
) -> dict[str, pa.Table]:
    """Build an atomic named bundle of Arrow tables from generated rows."""

    expected = set(schemas)
    supplied = set(rows_by_input)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise ValueError(f"bundle rows differ from schemas; missing={missing}, extra={extra}")
    return {name: table_from_rows(schema, rows_by_input[name]) for name, schema in schemas.items()}


__all__ = [
    "arrow_schema",
    "arrow_type",
    "infer_bundle_schema",
    "infer_schema",
    "key_capacity",
    "key_is_unique",
    "portable_dtype",
    "rows_satisfy_frame_constraints",
    "sort_rows_for_constraints",
    "table_from_rows",
    "tables_from_bundle_rows",
    "validate_bundle_schemas",
    "validate_frame_schema",
]
