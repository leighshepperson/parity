from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pytest
from hypothesis.errors import Unsatisfiable

import parity.shrinking as shrinking
from parity.models import (
    ColumnSchema,
    FrameSchema,
    GenerationConfig,
    InputBundle,
    InputSpec,
    KeyOverlap,
    KeyRef,
)
from parity.shrinking import (
    Counterexample,
    InputBundleCounterexample,
    find_bundle_counterexample,
    find_counterexample,
    find_unseen_bundle_counterexample,
    find_unseen_counterexample,
    hypothesis_settings,
    minimize_counterexample,
)


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


def test_find_unseen_counterexample_excludes_known_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_predicates: list[bool] = []

    def fake_find_counterexample(
        _schema: FrameSchema,
        predicate: Callable[[pa.Table], bool],
        _config: GenerationConfig | None,
    ) -> Counterexample | None:
        for value in (1, 2):
            table = pa.table({"x": [value]})
            accepted = predicate(table)
            seen_predicates.append(accepted)
            if accepted:
                return Counterexample(table)
        return None

    monkeypatch.setattr(shrinking, "find_counterexample", fake_find_counterexample)
    schema = FrameSchema(columns=[ColumnSchema(name="x", dtype="integer")], max_rows=1)

    result = find_unseen_counterexample(
        schema,
        lambda table: f"ms1:{table.column('x')[0].as_py()}",
        {"ms1:1"},
        GenerationConfig(max_examples=2),
    )

    assert result is not None
    assert result.table.column("x").to_pylist() == [2]
    assert seen_predicates == [False, True]


def test_find_unseen_counterexample_propagates_classifier_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_find_counterexample(
        _schema: FrameSchema,
        predicate: Callable[[pa.Table], bool],
        _config: GenerationConfig | None,
    ) -> Counterexample | None:
        predicate(pa.table({"x": [1]}))
        return None

    monkeypatch.setattr(shrinking, "find_counterexample", fake_find_counterexample)
    schema = FrameSchema(columns=[ColumnSchema(name="x", dtype="integer")], max_rows=1)

    with pytest.raises(RuntimeError, match="worker failed"):
        find_unseen_counterexample(
            schema,
            lambda _table: (_ for _ in ()).throw(RuntimeError("worker failed")),
            set(),
        )


def test_find_bundle_counterexample_uses_joint_strategy_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = {
        "orders": FrameSchema(columns=[ColumnSchema(name="id", dtype="integer")]),
        "customers": FrameSchema(columns=[ColumnSchema(name="id", dtype="integer")]),
    }
    bundle = object()
    strategy = object()
    observed_predicate: list[bool] = []

    monkeypatch.setattr(shrinking, "bundle_strategy", lambda value, value_schemas: strategy)

    def fake_find(
        selected_strategy: object,
        predicate: Callable[[dict[str, pa.Table]], bool],
        **_kwargs: object,
    ) -> dict[str, pa.Table]:
        assert selected_strategy is strategy
        tables = {
            "orders": pa.table({"id": [1]}),
            "customers": pa.table({"id": [1]}),
        }
        observed_predicate.append(predicate(tables))
        return tables

    monkeypatch.setattr(shrinking, "find", fake_find)
    result = find_bundle_counterexample(  # type: ignore[arg-type]
        bundle,
        schemas,
        lambda tables: list(tables) == ["orders", "customers"],
    )

    assert isinstance(result, InputBundleCounterexample)
    assert list(result.tables) == ["orders", "customers"]
    assert observed_predicate == [True]


def test_find_bundle_counterexample_explains_unsatisfiable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shrinking,
        "bundle_strategy",
        lambda _bundle, _schemas: object(),
    )
    monkeypatch.setattr(
        shrinking,
        "find",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Unsatisfiable()),
    )

    with pytest.raises(ValueError, match="relax input row bounds, key domains"):
        find_bundle_counterexample(  # type: ignore[arg-type]
            object(),
            {},
            lambda _tables: True,
        )


def test_find_unseen_bundle_counterexample_excludes_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tables = {"left": pa.table({"id": [1]}), "right": pa.table({"id": [1]})}
    captured: list[bool] = []

    def fake_find_bundle(
        _bundle: object,
        _schemas: object,
        predicate: Callable[[dict[str, pa.Table]], bool],
        _config: GenerationConfig | None,
    ) -> InputBundleCounterexample | None:
        captured.append(predicate(tables))
        return None

    monkeypatch.setattr(shrinking, "find_bundle_counterexample", fake_find_bundle)
    find_unseen_bundle_counterexample(  # type: ignore[arg-type]
        object(),
        {},
        lambda _tables: "ms1:known",
        {"ms1:known"},
    )

    assert captured == [False]


def test_find_bundle_counterexample_jointly_shrinks_relationship_bound_rows() -> None:
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
            max_rows=4,
        )
        for name in ("left", "right")
    }
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            KeyOverlap(
                left=KeyRef(input="left", columns=["id"]),
                right=KeyRef(input="right", columns=["id"]),
                min_shared=1,
            )
        ],
    )

    result = find_bundle_counterexample(
        bundle,
        schemas,
        lambda _tables: True,
        GenerationConfig(max_examples=50, derandomize=True),
    )

    assert result is not None
    assert result.tables["left"].num_rows == 1
    assert result.tables["right"].num_rows == 1
    assert result.tables["left"].column("id").to_pylist() == [0]
    assert result.tables["right"].column("id").to_pylist() == [0]
