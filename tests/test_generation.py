from __future__ import annotations

import math

import pandas as pd
import polars as pl
import pyarrow as pa
from hypothesis import given, settings

from parity.generation import adversarial_cases, column_strategy, frame_strategy
from parity.models import ColumnSchema, FrameSchema


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
