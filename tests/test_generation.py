from __future__ import annotations

import math
from typing import Literal

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest
from hypothesis import find, given, settings

from parity.generation import (
    adversarial_bundle_cases,
    adversarial_cases,
    bundle_strategy,
    column_strategy,
    frame_strategy,
)
from parity.models import (
    Cardinality,
    ColumnSchema,
    EqualRowCount,
    ForeignKey,
    FrameSchema,
    InputBundle,
    InputSpec,
    KeyOverlap,
    KeyRef,
    RowComparison,
    SortedBy,
)
from parity.schema import rows_satisfy_frame_constraints


def rich_schema() -> FrameSchema:
    return FrameSchema(
        columns=[
            ColumnSchema(name="id", dtype="integer", nullable=False),
            ColumnSchema(name="score", dtype="float"),
            ColumnSchema(name="group", dtype="category", categories=["a", "b"]),
            ColumnSchema(name="when", dtype="datetime", timezone="UTC"),
        ],
        min_rows=0,
        max_rows=8,
    )


def cross_width_equality_schema() -> FrameSchema:
    return FrameSchema(
        columns=[
            ColumnSchema(
                name="narrow",
                dtype="float32",
                nullable=False,
                minimum=-1,
                maximum=1,
            ),
            ColumnSchema(
                name="wide",
                dtype="float64",
                nullable=False,
                minimum=-1,
                maximum=1,
            ),
        ],
        min_rows=1,
        max_rows=5,
        constraints=[RowComparison(left="narrow", operator="eq", right="wide")],
    )


def relational_bundle() -> tuple[InputBundle, dict[str, FrameSchema]]:
    schemas = {
        "orders": FrameSchema(
            columns=[
                ColumnSchema(
                    name="customer_id",
                    dtype="integer",
                    nullable=False,
                    minimum=0,
                    maximum=5,
                ),
                ColumnSchema(name="amount", dtype="integer", nullable=False),
            ],
            min_rows=1,
            max_rows=6,
        ),
        "customers": FrameSchema(
            columns=[
                ColumnSchema(
                    name="id",
                    dtype="integer",
                    nullable=False,
                    unique=True,
                    minimum=0,
                    maximum=5,
                ),
                ColumnSchema(name="active", dtype="boolean", nullable=False),
            ],
            min_rows=1,
            max_rows=4,
        ),
    }
    orders = KeyRef(input="orders", columns=["customer_id"])
    customers = KeyRef(input="customers", columns=["id"])
    return (
        InputBundle(
            inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
            relationships=[
                KeyOverlap(left=orders, right=customers, min_shared=1),
                ForeignKey(child=orders, parent=customers, allow_nulls=False),
                Cardinality(left=orders, right=customers, relationship="many_to_one"),
            ],
        ),
        schemas,
    )


def test_adversarial_cases_cover_semantic_boundary_families() -> None:
    cases = adversarial_cases(rich_schema())
    names = [case.name for case in cases]
    assert names == [
        "empty",
        "singleton",
        "nulls",
        "nan-and-signed-zero",
        "duplicates",
        "extremes",
        "temporal-boundaries",
        "categories",
        "reversed-order",
    ]
    assert cases[0].table.num_rows == 0
    assert cases[1].table.num_rows == 1
    assert sum(column.null_count for column in cases[2].table.columns) > 0
    nan_values = cases[3].table.column("score").to_pylist()
    assert math.isnan(nan_values[0])
    assert nan_values[1] == 0.0
    assert math.copysign(1.0, nan_values[1]) < 0
    assert cases[4].table.slice(0, 1).equals(cases[4].table.slice(1, 1))
    assert isinstance(cases[1].as_adapter("pandas"), pd.DataFrame)
    assert isinstance(cases[1].as_adapter("polars"), pl.DataFrame)


def test_fixture_is_retained_as_first_deterministic_case() -> None:
    fixture = pa.table({"x": [10, 20]})
    cases = adversarial_cases(fixture=fixture)
    assert cases[0].name == "fixture"
    assert cases[0].table.equals(fixture)


def test_adversarial_extremes_stay_inside_categorical_domain() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="timestamp_text",
                dtype="string",
                nullable=False,
                categories=["2026-01-01T01:30:00Z", "2026-06-01T18:00:00Z"],
            )
        ],
        min_rows=1,
        max_rows=4,
    )
    extremes = next(case.table for case in adversarial_cases(schema) if case.name == "extremes")
    assert set(extremes.column("timestamp_text").to_pylist()) <= set(
        schema.columns[0].categories or []
    )


def test_adversarial_cases_respect_row_and_uniqueness_constraints() -> None:
    schema = FrameSchema(
        columns=[ColumnSchema(name="id", dtype="int64", nullable=False, unique=True)],
        min_rows=2,
        max_rows=4,
    )
    cases = adversarial_cases(schema)
    assert "empty" not in {case.name for case in cases}
    assert "singleton" not in {case.name for case in cases}
    assert "duplicates" not in {case.name for case in cases}
    for case in cases:
        values = case.table.column("id").to_pylist()
        assert 2 <= len(values) <= 4
        assert len(values) == len(set(values))


@given(
    frame_strategy(
        FrameSchema(
            columns=[
                ColumnSchema(name="group", dtype="integer", nullable=False, minimum=0, maximum=2),
                ColumnSchema(name="value", dtype="integer", minimum=0, maximum=3),
            ],
            max_rows=8,
            constraints=[SortedBy(columns=["group", "value"], nulls="last")],
        )
    )
)
@settings(max_examples=30, deadline=None)
def test_frame_strategy_constructs_composite_sorted_tables(table: pa.Table) -> None:
    rows = table.to_pylist()
    assert rows_satisfy_frame_constraints(
        FrameSchema(
            columns=[
                ColumnSchema(name="group", dtype="integer", nullable=False, minimum=0, maximum=2),
                ColumnSchema(name="value", dtype="integer", minimum=0, maximum=3),
            ],
            max_rows=8,
            constraints=[SortedBy(columns=["group", "value"], nulls="last")],
        ),
        rows,
    )


@given(
    frame_strategy(
        FrameSchema(
            columns=[ColumnSchema(name="value", dtype="integer", minimum=0, maximum=3)],
            max_rows=8,
            constraints=[SortedBy(columns=["value"], descending=True, nulls="first")],
        )
    )
)
@settings(max_examples=30, deadline=None)
def test_frame_strategy_honours_descending_sort_with_nulls_first(table: pa.Table) -> None:
    values = table.column("value").to_pylist()
    non_null = [value for value in values if value is not None]
    assert values[: values.count(None)] == [None] * values.count(None)
    assert non_null == sorted(non_null, reverse=True)


@given(
    frame_strategy(
        FrameSchema(
            columns=[
                ColumnSchema(name="start", dtype="integer", nullable=False, minimum=0, maximum=10),
                ColumnSchema(name="end", dtype="integer", nullable=False, minimum=0, maximum=10),
            ],
            min_rows=1,
            max_rows=8,
            constraints=[RowComparison(left="start", operator="le", right="end")],
        )
    )
)
@settings(max_examples=30, deadline=None)
def test_frame_strategy_constructs_start_before_end_rows(table: pa.Table) -> None:
    assert all(row["start"] <= row["end"] for row in table.to_pylist())


def test_category_to_unconstrained_string_equality_is_generated() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="left", dtype="string", nullable=False, categories=["a", "b"]),
            ColumnSchema(name="right", dtype="string", nullable=False),
        ],
        min_rows=1,
        max_rows=2,
        constraints=[RowComparison(left="left", operator="eq", right="right")],
    )

    table = find(
        frame_strategy(schema),
        lambda _table: True,
        settings=settings(max_examples=20, database=None, deadline=None, derandomize=True),
    )

    assert all(row["left"] == row["right"] for row in table.to_pylist())


def test_valid_row_comparison_examples_remain_adversarial_targets() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="start", dtype="integer", nullable=False, examples=[2]),
            ColumnSchema(name="end", dtype="integer", nullable=False, examples=[4]),
        ],
        min_rows=1,
        max_rows=3,
        constraints=[RowComparison(left="start", operator="le", right="end")],
    )

    singleton = next(case.table for case in adversarial_cases(schema) if case.name == "singleton")

    assert singleton.to_pylist() == [{"start": 2, "end": 4}]


@given(frame_strategy(cross_width_equality_schema()))
@settings(max_examples=100, deadline=None)
def test_frame_strategy_checks_constraints_after_arrow_width_cast(table: pa.Table) -> None:
    assert all(row["narrow"] == row["wide"] for row in table.to_pylist())


def test_adversarial_cases_check_constraints_after_arrow_width_cast() -> None:
    schema = cross_width_equality_schema()

    assert all(
        rows_satisfy_frame_constraints(schema, case.table.to_pylist())
        for case in adversarial_cases(schema)
    )


@pytest.mark.parametrize("operator", ["lt", "gt"])
def test_strict_cross_width_float_comparisons_use_representable_bounds(
    operator: Literal["lt", "gt"],
) -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="narrow",
                dtype="float32",
                nullable=False,
                minimum=-1,
                maximum=1,
            ),
            ColumnSchema(
                name="wide",
                dtype="float64",
                nullable=False,
                minimum=-1,
                maximum=1,
            ),
        ],
        min_rows=1,
        max_rows=4,
        constraints=[
            RowComparison(
                left="narrow",
                operator=operator,
                right="wide",
            )
        ],
    )

    table = find(
        frame_strategy(schema),
        lambda _table: True,
        settings=settings(max_examples=100, database=None, deadline=None, derandomize=True),
    )

    if operator == "lt":
        assert all(row["narrow"] < row["wide"] for row in table.to_pylist())
    else:
        assert all(row["narrow"] > row["wide"] for row in table.to_pylist())


def test_impossible_row_comparison_is_rejected_before_hypothesis() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="start", dtype="integer", nullable=False, minimum=5, maximum=6),
            ColumnSchema(name="end", dtype="integer", nullable=False, minimum=0, maximum=1),
        ],
        min_rows=1,
        constraints=[RowComparison(left="start", operator="le", right="end")],
    )

    with pytest.raises(ValueError, match="has no satisfying values"):
        frame_strategy(schema)


def test_adversarial_cases_preserve_declared_frame_constraints() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="start", dtype="integer", nullable=False, minimum=0, maximum=5),
            ColumnSchema(name="end", dtype="integer", nullable=False, minimum=0, maximum=5),
        ],
        min_rows=1,
        max_rows=4,
        constraints=[
            RowComparison(left="start", operator="le", right="end"),
            SortedBy(columns=["start", "end"], descending=True),
        ],
    )

    cases = adversarial_cases(schema)

    assert cases
    assert all(rows_satisfy_frame_constraints(schema, case.table.to_pylist()) for case in cases)


@given(column_strategy(ColumnSchema(name="score", dtype="float", minimum=5.0, maximum=7.0)))
@settings(max_examples=20, deadline=None)
def test_bounded_positive_float_strategy_is_valid(score: float | None) -> None:
    assert score is None or 5.0 <= score <= 7.0


@given(frame_strategy(rich_schema()))
@settings(max_examples=20, deadline=None)
def test_frame_strategy_respects_schema(table: pa.Table) -> None:
    assert table.column_names == ["id", "score", "group", "when"]
    assert 0 <= table.num_rows <= 8
    assert set(table.column("group").drop_null().to_pylist()) <= {"a", "b"}
    assert table.column("id").null_count == 0


@given(frame_strategy(rich_schema(), adapter="pandas"))
@settings(max_examples=3, deadline=None)
def test_frame_strategy_can_return_pandas(frame: pd.DataFrame) -> None:
    assert isinstance(frame, pd.DataFrame)


@given(frame_strategy(rich_schema(), adapter="polars"))
@settings(max_examples=3, deadline=None)
def test_frame_strategy_can_return_polars(frame: pl.DataFrame) -> None:
    assert isinstance(frame, pl.DataFrame)


@given(frame_strategy(FrameSchema(columns=[ColumnSchema(name="byte", dtype="uint8")], max_rows=5)))
@settings(max_examples=20, deadline=None)
def test_concrete_unsigned_generation_retains_dtype_and_domain(table: pa.Table) -> None:
    assert table.schema.field("byte").type == pa.uint8()
    assert all(0 <= value <= 255 for value in table.column("byte").drop_null().to_pylist())


@given(bundle_strategy(*relational_bundle()))
@settings(max_examples=30, deadline=None)
def test_bundle_strategy_jointly_preserves_overlap_foreign_key_and_cardinality(
    tables: dict[str, pa.Table],
) -> None:
    assert list(tables) == ["orders", "customers"]
    order_keys = tables["orders"].column("customer_id").to_pylist()
    customer_keys = tables["customers"].column("id").to_pylist()
    assert set(order_keys) <= set(customer_keys)
    assert set(order_keys) & set(customer_keys)
    assert len(customer_keys) == len(set(customer_keys))


def test_bundle_strategy_resorts_rows_after_relationship_key_assignment() -> None:
    schemas = {
        name: FrameSchema(
            columns=[
                ColumnSchema(
                    name="id",
                    dtype="integer",
                    nullable=False,
                    minimum=0,
                    maximum=3,
                )
            ],
            min_rows=2,
            max_rows=3,
            constraints=[SortedBy(columns=["id"], descending=True)],
        )
        for name in ("left", "right")
    }
    refs = {name: KeyRef(input=name, columns=["id"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[KeyOverlap(left=refs["left"], right=refs["right"])],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=50, database=None, deadline=None, derandomize=True),
    )

    for table in tables.values():
        values = table.column("id").to_pylist()
        assert values == sorted(values, reverse=True)


@given(
    bundle_strategy(
        InputBundle(
            inputs={
                name: InputSpec(input_schema=cross_width_equality_schema())
                for name in ("left", "right")
            }
        ),
        {name: cross_width_equality_schema() for name in ("left", "right")},
    )
)
@settings(max_examples=100, deadline=None)
def test_bundle_strategy_checks_constraints_after_arrow_width_cast(
    tables: dict[str, pa.Table],
) -> None:
    for table in tables.values():
        assert all(row["narrow"] == row["wide"] for row in table.to_pylist())


def test_adversarial_bundle_cases_are_atomic_and_relationship_valid() -> None:
    bundle, schemas = relational_bundle()
    fixtures = {
        "orders": pa.table({"customer_id": [2, 2], "amount": [10, 20]}),
        "customers": pa.table({"id": [2], "active": [True]}),
    }

    cases = adversarial_bundle_cases(bundle, schemas, fixtures=fixtures)

    assert cases[0].name == "fixture"
    assert list(cases[0].tables) == ["orders", "customers"]
    assert cases[0].tables["orders"].equals(fixtures["orders"])
    assert any(case.name == "singleton" for case in cases[1:])
    for case in cases[1:]:
        order_keys = set(case.tables["orders"].column("customer_id").to_pylist())
        customer_keys = set(case.tables["customers"].column("id").to_pylist())
        assert order_keys <= customer_keys
        assert order_keys & customer_keys


def test_adversarial_bundle_fixture_rejects_invalid_frame_constraints() -> None:
    schemas = {
        "left": FrameSchema(
            columns=[ColumnSchema(name="id", dtype="integer", nullable=False)],
            constraints=[SortedBy(columns=["id"])],
        ),
        "right": FrameSchema(columns=[ColumnSchema(name="id", dtype="integer", nullable=False)]),
    }
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()}
    )

    with pytest.raises(ValueError, match="does not satisfy its declared frame constraints"):
        adversarial_bundle_cases(
            bundle,
            schemas,
            fixtures={
                "left": pa.table({"id": [2, 1]}),
                "right": pa.table({"id": [1]}),
            },
        )


@given(
    bundle_strategy(
        InputBundle(
            inputs={
                "left": InputSpec(
                    input_schema=FrameSchema(
                        columns=[ColumnSchema(name="id", dtype="integer")], max_rows=4
                    )
                ),
                "right": InputSpec(
                    input_schema=FrameSchema(
                        columns=[ColumnSchema(name="id", dtype="integer")], max_rows=4
                    )
                ),
            },
            relationships=[EqualRowCount(inputs=["left", "right"])],
        ),
        {
            "left": FrameSchema(columns=[ColumnSchema(name="id", dtype="integer")], max_rows=4),
            "right": FrameSchema(columns=[ColumnSchema(name="id", dtype="integer")], max_rows=4),
        },
    )
)
@settings(max_examples=20, deadline=None)
def test_bundle_strategy_preserves_equal_row_count(tables: dict[str, pa.Table]) -> None:
    assert tables["left"].num_rows == tables["right"].num_rows


def _categorical_key_schema(
    categories: list[str | None],
    *,
    min_rows: int = 1,
    max_rows: int = 3,
    nullable: bool = False,
) -> FrameSchema:
    return FrameSchema(
        columns=[
            ColumnSchema(
                name="key",
                dtype="string",
                categories=categories,
                nullable=nullable,
            )
        ],
        min_rows=min_rows,
        max_rows=max_rows,
    )


def test_bundle_strategy_preserves_non_transitive_overlap_edges() -> None:
    schemas = {
        "left": _categorical_key_schema(["left"], max_rows=1),
        "middle": _categorical_key_schema(["left", "right"], min_rows=2, max_rows=2),
        "right": _categorical_key_schema(["right"], max_rows=1),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            KeyOverlap(left=refs["left"], right=refs["middle"]),
            KeyOverlap(left=refs["middle"], right=refs["right"]),
        ],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=50, database=None, deadline=None, derandomize=True),
    )

    assert tables["left"].column("key").to_pylist() == ["left"]
    assert set(tables["middle"].column("key").to_pylist()) == {"left", "right"}
    assert tables["right"].column("key").to_pylist() == ["right"]


def test_bundle_strategy_can_generate_unshared_residual_keys() -> None:
    schemas = {
        name: _categorical_key_schema(["shared", "left", "right"], min_rows=3, max_rows=3)
        for name in ("left", "right")
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[KeyOverlap(left=refs["left"], right=refs["right"], min_shared=1)],
    )

    def has_residuals(tables: dict[str, pa.Table]) -> bool:
        left = set(tables["left"].column("key").to_pylist())
        right = set(tables["right"].column("key").to_pylist())
        return bool(left & right and left - right and right - left)

    tables = find(
        bundle_strategy(bundle, schemas),
        has_residuals,
        settings=settings(max_examples=500, database=None, deadline=None, derandomize=True),
    )

    assert has_residuals(tables)


def test_bundle_strategy_generates_null_foreign_key_with_empty_parent() -> None:
    schemas = {
        "child": _categorical_key_schema(["parent", None], nullable=True, max_rows=1),
        "parent": _categorical_key_schema(["parent"], min_rows=0, max_rows=0),
    }
    child = KeyRef(input="child", columns=["key"])
    parent = KeyRef(input="parent", columns=["key"])
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[ForeignKey(child=child, parent=parent, allow_nulls=True)],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=20, database=None, deadline=None, derandomize=True),
    )

    assert tables["child"].column("key").to_pylist() == [None]
    assert tables["parent"].num_rows == 0


def test_cardinality_does_not_force_shared_values_during_generation() -> None:
    schemas = {
        "left": _categorical_key_schema(["left"]),
        "right": _categorical_key_schema(["right"]),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            Cardinality(left=refs["left"], right=refs["right"], relationship="many_to_many")
        ],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=20, database=None, deadline=None, derandomize=True),
    )

    assert tables["left"].column("key").to_pylist() == ["left"]
    assert tables["right"].column("key").to_pylist() == ["right"]


def test_adversarial_bundle_rejects_fixture_that_violates_relationships() -> None:
    bundle, schemas = relational_bundle()
    fixtures = {
        "orders": pa.table({"customer_id": [2], "amount": [10]}),
        "customers": pa.table({"id": [1], "active": [True]}),
    }

    with pytest.raises(ValueError, match="do not satisfy"):
        adversarial_bundle_cases(bundle, schemas, fixtures=fixtures)


def test_bundle_strategy_intersects_multiple_foreign_key_parents() -> None:
    schemas = {
        "child": _categorical_key_schema(["shared"], max_rows=1),
        "first_parent": FrameSchema(
            columns=[
                ColumnSchema(
                    name="key",
                    dtype="string",
                    nullable=False,
                    unique=True,
                    categories=["shared", "first"],
                )
            ],
            min_rows=2,
            max_rows=2,
        ),
        "second_parent": FrameSchema(
            columns=[
                ColumnSchema(
                    name="key",
                    dtype="string",
                    nullable=False,
                    unique=True,
                    categories=["shared", "second"],
                )
            ],
            min_rows=2,
            max_rows=2,
        ),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            ForeignKey(child=refs["child"], parent=refs["first_parent"], allow_nulls=False),
            ForeignKey(child=refs["child"], parent=refs["second_parent"], allow_nulls=False),
        ],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=50, database=None, deadline=None, derandomize=True),
    )

    child = set(tables["child"].column("key").to_pylist())
    assert child <= set(tables["first_parent"].column("key").to_pylist())
    assert child <= set(tables["second_parent"].column("key").to_pylist())


def test_bundle_strategy_uses_null_for_disjoint_nullable_foreign_key_parents() -> None:
    schemas = {
        "child": _categorical_key_schema(["first", "second", None], nullable=True, max_rows=1),
        "first_parent": _categorical_key_schema(["first"], max_rows=1),
        "second_parent": _categorical_key_schema(["second"], max_rows=1),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            ForeignKey(child=refs["child"], parent=refs["first_parent"], allow_nulls=True),
            ForeignKey(child=refs["child"], parent=refs["second_parent"], allow_nulls=True),
        ],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=20, database=None, deadline=None, derandomize=True),
    )

    assert tables["child"].column("key").to_pylist() == [None]


def test_bundle_strategy_propagates_overlap_through_foreign_key_parent() -> None:
    schemas = {
        "child": _categorical_key_schema(["shared"], max_rows=1),
        "parent": _categorical_key_schema(["shared", "other"], max_rows=1),
        "peer": _categorical_key_schema(["shared"], max_rows=1),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            ForeignKey(child=refs["child"], parent=refs["parent"], allow_nulls=False),
            KeyOverlap(left=refs["child"], right=refs["peer"]),
        ],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=20, database=None, deadline=None, derandomize=True),
    )

    assert tables["child"].column("key").to_pylist() == ["shared"]
    assert tables["parent"].column("key").to_pylist() == ["shared"]
    assert tables["peer"].column("key").to_pylist() == ["shared"]


def test_nullable_key_value_counts_toward_cardinality_capacity() -> None:
    schemas = {
        "left": _categorical_key_schema(["value", None], min_rows=2, max_rows=2, nullable=True),
        "right": _categorical_key_schema(["other"], max_rows=1),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            Cardinality(left=refs["left"], right=refs["right"], relationship="one_to_many")
        ],
    )

    tables = find(
        bundle_strategy(bundle, schemas),
        lambda _tables: True,
        settings=settings(max_examples=20, database=None, deadline=None, derandomize=True),
    )

    assert set(tables["left"].column("key").to_pylist()) == {"value", None}
