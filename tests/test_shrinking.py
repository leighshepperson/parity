from __future__ import annotations

import pyarrow as pa
import pytest

import parity.shrinking as shrinking
from parity.models import ColumnSchema, FrameSchema, GenerationConfig
from parity.shrinking import find_counterexample, hypothesis_settings, minimize_counterexample


def test_find_counterexample_shrinks_row_count_and_value() -> None:
    schema = FrameSchema(
        columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
        max_rows=8,
    )
    result = find_counterexample(
        schema,
        lambda table: (
            table.num_rows > 0 and any(value > 10 for value in table.column("x").to_pylist())
        ),
        GenerationConfig(max_examples=500, derandomize=True),
    )
    assert result is not None
    assert result.table.num_rows == 1
    assert result.table.column("x").to_pylist() == [11]
    assert result.source == "generated:shrunk"


def test_find_counterexample_returns_none_when_property_never_fails() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="x", dtype="boolean")], max_rows=3)
    assert (
        find_counterexample(
            schema,
            lambda _table: False,
            GenerationConfig(max_examples=10, derandomize=True),
        )
        is None
    )


def test_minimize_requires_failing_initial_example() -> None:
    with pytest.raises(ValueError, match="does not satisfy"):
        minimize_counterexample(pa.table({"x": [1]}), lambda _table: False)


def test_hypothesis_settings_have_no_persistent_database() -> None:
    configured = hypothesis_settings(GenerationConfig(max_examples=7, deadline_ms=20))
    assert configured.max_examples == 7
    assert configured.deadline.total_seconds() == 0.02
    assert configured.database is None


def test_seed_is_forwarded_as_reproducible_random_source(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[float] = []

    def fake_find(*_args: object, **kwargs: object) -> pa.Table:
        generator = kwargs["random"]
        observed.append(generator.random())  # type: ignore[union-attr]
        return pa.table({"x": [1]})

    monkeypatch.setattr(shrinking, "find", fake_find)
    schema = FrameSchema(columns=[ColumnSchema(name="x", dtype="integer")], max_rows=1)
    for _ in range(2):
        shrinking.find_counterexample(schema, lambda _table: True, GenerationConfig(seed=101))
    assert observed[0] == observed[1]
    assert hypothesis_settings(GenerationConfig(seed=101)).derandomize is False
