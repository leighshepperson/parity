from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

import parity.engine as engine
from parity.artifacts import ArtifactStore
from parity.config import load_config
from parity.engine import replay_artifact, run_live
from parity.execution import (
    ExceptionInfo,
    ExecutionOutcome,
    IsolatedExecutionSession,
    Observation,
)
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseProvenance,
    CaseResult,
    ColumnSchema,
    ComparisonPolicy,
    ExampleResult,
    FrameSchema,
    GenerationConfig,
    InputBundle,
    InputSpec,
    Mismatch,
    MismatchKind,
    PandasInput,
    ParityConfig,
    PerformanceConfig,
    RunMetrics,
    SortedBy,
    Status,
)
from parity.provenance import collect_runtime_provenance
from parity.reporting import render_terminal


def identity(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.copy()


def corrupt_seven(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result.loc[result["x"] == 7, "x"] = 8
    return result


def corrupt_everything(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["x"] = result["x"] + 1
    return result


_live_stateful_calls = 0


def corrupt_after_live_warmup(frame: pd.DataFrame) -> pd.DataFrame:
    global _live_stateful_calls
    _live_stateful_calls += 1
    result = frame.copy()
    if _live_stateful_calls > 2:
        result["x"] = result["x"] + 1
    return result


def return_unsupported(_frame: pd.DataFrame) -> object:
    return object()


def return_complex_dataframe(_frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"complex": [1 + 2j]})


def return_integer_mapping_key(_frame: pd.DataFrame) -> dict[object, str]:
    return {1: "same"}


def return_string_mapping_key(_frame: pd.DataFrame) -> dict[object, str]:
    return {"1": "same"}


def corrupt_two_shapes(frame: pd.DataFrame) -> pd.DataFrame:
    """Expose two independent semantic symptoms for multi-finding tests."""

    result = frame.copy()
    if (result["x"] == 0).any():
        return result.rename(columns={"x": "renamed"})
    if (result["x"] > 0).any():
        result["x"] = result["x"] + 1
    return result


def merge_named(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    return left.merge(right, on="key", how="left", sort=True)


def unannotated_polars_identity(frame):
    assert isinstance(frame, pl.DataFrame)
    return frame


def _dense_union_table() -> pa.Table:
    values = pa.UnionArray.from_dense(
        pa.array([0, 1, 0], type=pa.int8()),
        pa.array([0, 0, 1], type=pa.int32()),
        [pa.array([1, 2], type=pa.int64()), pa.array(["text"])],
        field_names=["number", "text"],
    )
    return pa.Table.from_arrays([values], names=["mixed"])


def arrow_chunk_count(frame: pa.Table) -> dict[str, int]:
    return {"chunks": max(1, frame.column(0).num_chunks)}


def arrow_one_chunk(_frame: pa.Table) -> dict[str, int]:
    return {"chunks": 1}


class BoundTransform:
    def corrupt(self, frame: pd.DataFrame) -> pd.DataFrame:
        return corrupt_everything(frame)


def _run(
    tmp_path: Path,
    candidate,
    *,
    generation: GenerationConfig,
):
    return run_live(
        identity,
        candidate,
        fixture=None,
        schema=FrameSchema(
            columns=[
                ColumnSchema(
                    name="x",
                    dtype="integer",
                    nullable=False,
                    minimum=0,
                    maximum=20,
                )
            ],
            max_rows=5,
        ),
        comparison=ComparisonPolicy(),
        generation=generation,
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
    )


def test_live_engine_consumes_generated_strategy_when_adversarial_disabled(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        corrupt_seven,
        generation=GenerationConfig(
            max_examples=500,
            seed=71,
            adversarial_examples=False,
        ),
    )
    case = result.cases[0]
    assert result.status is Status.FAILED
    assert case.deterministic_examples == 0
    assert case.generated_examples > 0
    assert len(case.failures) == 1
    assert case.failures[0].source == "generated:shrunk"
    assert case.failures[0].artifact is not None


def test_live_engine_passes_equal_functions_across_both_generation_layers(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        identity,
        generation=GenerationConfig(max_examples=20, seed=72),
    )
    case = result.cases[0]
    assert result.status is Status.PASSED
    assert case.deterministic_examples > 0
    assert case.generated_examples >= 20
    assert case.failures == []


def test_live_engine_accepts_explicit_cross_engine_adapters(tmp_path: Path) -> None:
    result = run_live(
        identity,
        unannotated_polars_identity,
        fixture=pd.DataFrame({"x": [1, 2]}),
        schema=None,
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=3, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="polars",
    )

    assert result.status is Status.PASSED


def test_live_engine_can_run_only_the_exact_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_search(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("property search must be disabled")

    monkeypatch.setattr(engine, "find_unseen_counterexample", unexpected_search)
    result = run_live(
        identity,
        identity,
        fixture=pd.DataFrame({"x": [7]}),
        schema=None,
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(
            search=False,
            adversarial_examples=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
    )

    case = result.cases[0]
    assert result.status is Status.PASSED
    assert case.examples_run == case.deterministic_examples == 1
    assert case.generated_examples == 0
    assert case.failures == []


def test_live_engine_rejects_searchless_campaign_without_deterministic_inputs(
    tmp_path: Path,
) -> None:
    def local_identity(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.copy()

    with pytest.raises(ValueError, match="requires at least one deterministic input"):
        run_live(
            local_identity,
            local_identity,
            fixture=None,
            schema=FrameSchema(columns=[ColumnSchema(name="x", dtype="int64", nullable=False)]),
            comparison=ComparisonPolicy(),
            generation=GenerationConfig(
                search=False,
                adversarial_examples=False,
            ),
            performance=PerformanceConfig(enabled=False),
            artifact_dir=tmp_path,
        )


def test_live_engine_accepts_named_input_bundle(tmp_path: Path) -> None:
    result = run_live(
        merge_named,
        merge_named,
        fixture=None,
        schema=None,
        input_fixtures={
            "left": pd.DataFrame({"key": [1, 2]}),
            "right": pd.DataFrame({"key": [1], "value": [10]}),
        },
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=2, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
    )

    assert result.status is Status.PASSED
    assert result.cases[0].generated_examples >= 2


def test_live_engine_rejects_bundle_options_for_single_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="apply only"):
        run_live(
            identity,
            identity,
            fixture=pd.DataFrame({"x": [1]}),
            schema=None,
            input_binding="positional",
            comparison=ComparisonPolicy(),
            generation=GenerationConfig(max_examples=1, adversarial_examples=False),
            performance=PerformanceConfig(enabled=False),
            artifact_dir=tmp_path,
        )


def test_live_engine_rejects_invalid_distribution_names_before_execution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="distribution names"):
        run_live(
            identity,
            identity,
            fixture=pd.DataFrame({"x": [1]}),
            schema=None,
            comparison=ComparisonPolicy(),
            generation=GenerationConfig(max_examples=1, adversarial_examples=False),
            performance=PerformanceConfig(enabled=False),
            artifact_dir=tmp_path,
            reference_distributions=["bad/name"],
            candidate_distributions=["bad/name"],
        )


def test_live_engine_propagates_independent_pandas_input_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, PandasInput]] = []

    def execute(
        function: Any,
        _table: Any,
        *,
        adapter: str = "auto",
        pandas_input: PandasInput = "arrow",
        record_distributions: Sequence[str] = (),
    ) -> Observation:
        assert adapter == "pandas"
        seen.append((function.__name__, pandas_input))
        assert not record_distributions
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            metrics=RunMetrics(duration_seconds=0),
            value={"same": True},
            has_value=True,
        )

    monkeypatch.setattr(engine, "execute_callable_current", execute)
    result = run_live(
        identity,
        corrupt_everything,
        fixture=pd.DataFrame({"x": [1, 2]}),
        schema=None,
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
        reference_pandas_input="arrow",
        candidate_pandas_input="native",
    )

    assert result.status is Status.PASSED
    assert set(seen) == {("identity", "arrow"), ("corrupt_everything", "native")}


def test_live_arrow_fixture_chunk_layout_matches_replay(tmp_path: Path) -> None:
    chunked = pa.Table.from_batches([pa.record_batch({"x": [1]}), pa.record_batch({"x": [2]})])
    assert chunked.column(0).num_chunks == 2

    result = run_live(
        arrow_chunk_count,
        arrow_one_chunk,
        fixture=chunked,
        schema=None,
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(
            max_examples=1, adversarial_examples=False, suppress_too_slow=False
        ),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="arrow",
        candidate_adapter="arrow",
    )

    assert result.status is Status.PASSED


def test_live_importable_failure_preserves_contract_for_replay(tmp_path: Path) -> None:
    result = run_live(
        identity,
        corrupt_everything,
        fixture=pd.DataFrame({"x": [1, 2]}),
        schema=None,
        comparison=ComparisonPolicy(dtype="strict"),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
        reference_pandas_input="arrow",
        candidate_pandas_input="native",
    )
    artifact = result.cases[0].failures[0].artifact

    assert artifact is not None
    replay_payload = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    replay_contract = replay_payload["case"]
    assert replay_payload["version"] == 1
    assert replay_payload["expected_runtime"]["reference"]["python_version"]
    assert len(replay_payload["config_sha256"]) == 64
    assert replay_contract["reference"]["adapter"] == "pandas"
    assert replay_contract["candidate"]["adapter"] == "pandas"
    assert replay_contract["reference"]["pandas_input"] == "arrow"
    assert replay_contract["candidate"]["pandas_input"] == "native"
    assert replay_contract["comparison"]["dtype"] == "strict"
    replayed = replay_artifact(artifact)
    assert replayed.status is Status.FAILED
    assert replayed.cases[0].provenance is not None
    assert replayed.cases[0].provenance.verification == "verified"


def test_live_bound_instance_method_is_not_claimed_as_replayable(tmp_path: Path) -> None:
    result = run_live(
        identity,
        BoundTransform().corrupt,
        fixture=pd.DataFrame({"x": [1]}),
        schema=None,
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
    )
    artifact = result.cases[0].failures[0].artifact

    assert artifact is not None
    replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    replay_contract = replay["case"]
    assert replay_contract["candidate"] is None
    assert "command" not in replay
    with pytest.raises(engine.ReplayError, match="live-callable"):
        replay_artifact(artifact)


def test_rebound_live_function_is_not_claimed_as_replayable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = corrupt_everything
    monkeypatch.setattr(
        engine.inspect.getmodule(corrupt_everything), "corrupt_everything", identity
    )

    assert engine._importable_spec(original, adapter="pandas") is None


def test_deterministic_witness_skips_redundant_property_search(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        corrupt_everything,
        generation=GenerationConfig(max_examples=500, seed=73),
    )
    case = result.cases[0]
    assert result.status is Status.FAILED
    assert case.failures
    assert case.generated_examples == 0


def test_engine_discovers_and_orders_two_distinct_mismatch_signatures(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        corrupt_two_shapes,
        generation=GenerationConfig(
            max_examples=500,
            max_findings=2,
            seed=74,
            adversarial_examples=False,
        ),
    )

    case = result.cases[0]
    signatures = [failure.finding_signature for failure in case.failures]
    assert result.status is Status.FAILED
    assert case.findings_discovered == 2
    assert len(set(signatures)) == 2
    assert signatures == sorted(signatures)  # type: ignore[arg-type]
    assert all(signature and signature.startswith("ms1:") for signature in signatures)


def test_default_finding_budget_stops_after_first_distinct_signature(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        corrupt_two_shapes,
        generation=GenerationConfig(
            max_examples=500,
            seed=75,
            adversarial_examples=False,
        ),
    )

    assert result.cases[0].findings_discovered == 1
    assert len(result.cases[0].failures) == 1


def test_repeated_witnesses_with_one_signature_are_deduplicated(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        corrupt_everything,
        generation=GenerationConfig(
            max_examples=20,
            max_findings=3,
            seed=76,
        ),
    )

    case = result.cases[0]
    assert result.status is Status.FAILED
    assert case.findings_discovered == 1
    assert len(case.failures) == 1


def test_deterministic_side_nondeterminism_is_an_error(tmp_path: Path) -> None:
    candidate_calls = 0

    def reference_runner(value: pa.Table) -> Observation:
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            table=value,
            metrics=RunMetrics(duration_seconds=0),
        )

    def candidate_runner(_value: pa.Table) -> Observation:
        nonlocal candidate_calls
        candidate_calls += 1
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            table=pa.table({"x": [candidate_calls + 1]}),
            metrics=RunMetrics(duration_seconds=0),
        )

    result = engine._campaign(
        name="nondeterministic",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=reference_runner,
        candidate_runner=candidate_runner,
        artifact_case="nondeterministic",
    )

    assert result.status is Status.ERROR
    assert result.findings_discovered == 0
    assert "nondeterministic on the candidate side" in result.failures[0].mismatches[0].message


def test_stability_probe_detects_synchronized_changing_outputs_and_stops(
    tmp_path: Path,
) -> None:
    calls = {"reference": 0, "candidate": 0}
    benchmark_called = False

    def runner(label: str, _value: pa.Table) -> Observation:
        calls[label] += 1
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            value={"call": calls[label]},
            has_value=True,
            metrics=RunMetrics(duration_seconds=0),
        )

    def benchmark(_value: Any) -> None:
        nonlocal benchmark_called
        benchmark_called = True

    result = engine._campaign(
        name="synchronized-state",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(
            max_examples=20,
            adversarial_examples=False,
            stability_repeats=4,
        ),
        performance_config=PerformanceConfig(enabled=True),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=lambda value: runner("reference", value),
        candidate_runner=lambda value: runner("candidate", value),
        artifact_case="synchronized-state",
        benchmark=benchmark,  # type: ignore[arg-type]
    )

    failure = result.failures[0]
    assert result.status is Status.ERROR
    assert result.findings_discovered == 0
    assert result.diagnoses == []
    assert failure.finding_signature is None
    assert failure.source == "deterministic:stability:reference,candidate:repeat-2"
    assert [mismatch.path for mismatch in failure.mismatches] == [
        "$reference.stability[2]",
        "$candidate.stability[2]",
    ]
    assert all("repeat 2" in mismatch.message for mismatch in failure.mismatches)
    assert calls == {"reference": 2, "candidate": 2}
    assert result.examples_run == result.deterministic_examples == 1
    assert result.generated_examples == 0
    assert result.performance is None
    assert not benchmark_called


def test_stability_probe_attributes_one_sided_drift(tmp_path: Path) -> None:
    candidate_calls = 0

    def stable(_value: pa.Table) -> Observation:
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            value={"call": 1},
            has_value=True,
            metrics=RunMetrics(duration_seconds=0),
        )

    def changing(_value: pa.Table) -> Observation:
        nonlocal candidate_calls
        candidate_calls += 1
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            value={"call": candidate_calls},
            has_value=True,
            metrics=RunMetrics(duration_seconds=0),
        )

    result = engine._campaign(
        name="one-sided-state",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, stability_repeats=3),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=stable,
        candidate_runner=changing,
        artifact_case="one-sided-state",
        exact_only=True,
    )

    failure = result.failures[0]
    assert result.status is Status.ERROR
    assert failure.source == "deterministic:stability:candidate:repeat-2"
    assert [mismatch.path for mismatch in failure.mismatches] == ["$candidate.stability[2]"]
    assert candidate_calls == 2


def test_stability_probe_allows_stable_outputs_for_configured_repeat_count(
    tmp_path: Path,
) -> None:
    calls = {"reference": 0, "candidate": 0}

    def runner(label: str, _value: pa.Table) -> Observation:
        calls[label] += 1
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            value={"stable": True},
            has_value=True,
            metrics=RunMetrics(duration_seconds=0),
        )

    result = engine._campaign(
        name="stable",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, stability_repeats=3),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=lambda value: runner("reference", value),
        candidate_runner=lambda value: runner("candidate", value),
        artifact_case="stable",
        exact_only=True,
    )

    assert result.status is Status.PASSED
    assert result.failures == []
    assert calls == {"reference": 3, "candidate": 3}
    assert result.examples_run == 1


def test_stability_repeats_one_disables_repeat_observations(tmp_path: Path) -> None:
    calls = {"reference": 0, "candidate": 0}

    def changing(label: str, _value: pa.Table) -> Observation:
        calls[label] += 1
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            value={"call": calls[label]},
            has_value=True,
            metrics=RunMetrics(duration_seconds=0),
        )

    result = engine._campaign(
        name="stability-disabled",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, stability_repeats=1),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=lambda value: changing("reference", value),
        candidate_runner=lambda value: changing("candidate", value),
        artifact_case="stability-disabled",
        exact_only=True,
    )

    assert result.status is Status.PASSED
    assert calls == {"reference": 1, "candidate": 1}


def test_fixture_constraints_apply_when_adversarial_examples_are_disabled(
    tmp_path: Path,
) -> None:
    schema = FrameSchema(
        columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
        constraints=[SortedBy(columns=["x"])],
    )

    with pytest.raises(ValueError, match="fixture does not satisfy"):
        engine._campaign(
            name="invalid-fixture",
            schema=schema,
            fixture=pa.table({"x": [2, 1]}),
            comparison=ComparisonPolicy(),
            generation=GenerationConfig(
                max_examples=1,
                adversarial_examples=False,
                stability_repeats=1,
            ),
            performance_config=PerformanceConfig(enabled=False),
            artifact_store=ArtifactStore(tmp_path),
            reference_runner=lambda _value: pytest.fail("invalid fixture was executed"),
            candidate_runner=lambda _value: pytest.fail("invalid fixture was executed"),
            artifact_case="invalid-fixture",
        )


def test_bundle_fixture_constraints_apply_when_adversarial_examples_are_disabled(
    tmp_path: Path,
) -> None:
    schemas = {
        "left": FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            constraints=[SortedBy(columns=["x"])],
        ),
        "right": FrameSchema(columns=[ColumnSchema(name="x", dtype="integer", nullable=False)]),
    }
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()}
    )

    with pytest.raises(ValueError, match="does not satisfy its declared frame constraints"):
        engine._campaign(
            name="invalid-bundle-fixture",
            schema=None,
            fixture={"left": pa.table({"x": [2, 1]}), "right": pa.table({"x": [1]})},
            input_bundle=bundle,
            bundle_schemas=schemas,
            comparison=ComparisonPolicy(),
            generation=GenerationConfig(
                max_examples=1,
                adversarial_examples=False,
                stability_repeats=1,
            ),
            performance_config=PerformanceConfig(enabled=False),
            artifact_store=ArtifactStore(tmp_path),
            reference_runner=lambda _value: pytest.fail("invalid fixture was executed"),
            candidate_runner=lambda _value: pytest.fail("invalid fixture was executed"),
            artifact_case="invalid-bundle-fixture",
        )


def test_stability_probe_detects_exception_contract_drift(tmp_path: Path) -> None:
    candidate_calls = 0

    def raised(message: str) -> Observation:
        return Observation(
            outcome=ExecutionOutcome.RAISED,
            exception=ExceptionInfo("builtins", "ValueError", message),
            metrics=RunMetrics(duration_seconds=0),
        )

    def candidate(_value: pa.Table) -> Observation:
        nonlocal candidate_calls
        candidate_calls += 1
        return raised(f"attempt {candidate_calls}")

    result = engine._campaign(
        name="exception-state",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, stability_repeats=2),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=lambda _value: raised("attempt 1"),
        candidate_runner=candidate,
        artifact_case="exception-state",
        exact_only=True,
    )

    failure = result.failures[0]
    assert result.status is Status.ERROR
    assert failure.finding_signature is None
    assert failure.source == "deterministic:stability:candidate:repeat-2"
    assert failure.mismatches[0].message == "candidate changed on stability repeat 2"
    assert "attempt" not in failure.mismatches[0].message


def test_stability_probe_sanitizes_repeat_infrastructure_failure(tmp_path: Path) -> None:
    candidate_calls = 0

    def returned(_value: pa.Table) -> Observation:
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            value={"stable": True},
            has_value=True,
            metrics=RunMetrics(duration_seconds=0),
        )

    def candidate(_value: pa.Table) -> Observation:
        nonlocal candidate_calls
        candidate_calls += 1
        if candidate_calls == 1:
            return returned(_value)
        return Observation(
            outcome=ExecutionOutcome.CRASHED,
            exception=ExceptionInfo("parity.execution", "WorkerError", "private worker output"),
            metrics=RunMetrics(duration_seconds=0),
        )

    result = engine._campaign(
        name="repeat-crash",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, stability_repeats=2),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=returned,
        candidate_runner=candidate,
        artifact_case="repeat-crash",
        exact_only=True,
    )

    failure = result.failures[0]
    assert result.status is Status.ERROR
    assert failure.source == "deterministic:stability:candidate:repeat-2"
    assert failure.mismatches[0].message == "candidate changed on stability repeat 2"
    assert "private worker output" not in failure.mismatches[0].message


def test_configured_stateful_failure_is_not_accepted_from_a_warmed_session(
    tmp_path: Path,
) -> None:
    (tmp_path / "stateful_transforms.py").write_text(
        """
_calls = 0

def reference(frame):
    return frame.copy()

def candidate(frame):
    global _calls
    _calls += 1
    result = frame.copy()
    if _calls > 2:
        result["x"] = result["x"] + 1
    return result
""",
        encoding="utf-8",
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="stateful",
                reference=CallableSpec(
                    target="stateful_transforms:reference",
                    adapter="pandas",
                    workdir=tmp_path,
                ),
                candidate=CallableSpec(
                    target="stateful_transforms:candidate",
                    adapter="pandas",
                    workdir=tmp_path,
                ),
                input_schema=FrameSchema(
                    columns=[
                        ColumnSchema(
                            name="x",
                            dtype="integer",
                            nullable=False,
                            minimum=0,
                            maximum=1,
                        )
                    ],
                    max_rows=5,
                ),
                generation=GenerationConfig(max_examples=20, derandomize=True),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert case.failures[0].finding_signature is None
    assert "not reproducible" in case.failures[0].mismatches[0].message


def test_configured_stability_error_replays_with_saved_repeat_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "changing_transforms.py").write_text(
        """
_reference_calls = 0
_candidate_calls = 0

def reference(_frame):
    global _reference_calls
    _reference_calls += 1
    return {"call": _reference_calls}

def candidate(_frame):
    global _candidate_calls
    _candidate_calls += 1
    return {"call": _candidate_calls}
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"x": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    monkeypatch.chdir(tmp_path)
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="replay-stability",
                reference=CallableSpec(
                    target="changing_transforms:reference",
                    adapter="pandas",
                    workdir=tmp_path,
                ),
                candidate=CallableSpec(
                    target="changing_transforms:candidate",
                    adapter="pandas",
                    workdir=tmp_path,
                ),
                fixture=fixture,
                generation=GenerationConfig(
                    max_examples=1,
                    adversarial_examples=False,
                    stability_repeats=3,
                ),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    failure = result.cases[0].failures[0]
    assert result.status is Status.ERROR
    assert failure.source == "deterministic:stability:reference,candidate:repeat-2"
    assert failure.artifact is not None
    replay_contract = json.loads((failure.artifact / "replay.json").read_text(encoding="utf-8"))
    assert replay_contract["case"]["generation"]["stability_repeats"] == 3

    replayed = replay_artifact(failure.artifact)

    replay_failure = replayed.cases[0].failures[0]
    assert replayed.status is Status.ERROR
    assert replay_failure.source == "deterministic:stability:reference,candidate:repeat-2"
    assert replay_failure.artifact == failure.artifact


def test_configured_campaign_passes_shared_and_endpoint_specific_kwargs(
    tmp_path: Path,
) -> None:
    (tmp_path / "endpoint_transform.py").write_text(
        """
from pathlib import Path

def transform(frame, *, scale, engine, log_path):
    assert scale == 2
    with Path(log_path).open("a", encoding="utf-8") as stream:
        stream.write(engine + "\\n")
    return frame
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"x": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    reference_log = tmp_path / "reference.log"
    candidate_log = tmp_path / "candidate.log"
    case = CaseConfig(
        name="endpoint-kwargs",
        reference=CallableSpec(
            target="endpoint_transform:transform",
            adapter="arrow",
            workdir=tmp_path,
        ),
        candidate=CallableSpec(
            target="endpoint_transform:transform",
            adapter="arrow",
            workdir=tmp_path,
        ),
        fixture=fixture,
        static_kwargs={"scale": 2},
        reference_kwargs={"engine": "pandas", "log_path": str(reference_log)},
        candidate_kwargs={"engine": "polars", "log_path": str(candidate_log)},
        generation=GenerationConfig(
            max_examples=1,
            adversarial_examples=False,
            search=False,
            stability_repeats=2,
        ),
        performance=PerformanceConfig(
            enabled=True,
            warmups=0,
            repeats=1,
            max_slowdown=None,
            max_memory_ratio=None,
            min_reference_ms=0,
        ),
    )

    result = engine.run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    assert result.status is Status.PASSED
    assert reference_log.read_text(encoding="utf-8").splitlines() == ["pandas"] * 3
    assert candidate_log.read_text(encoding="utf-8").splitlines() == ["polars"] * 3


def test_endpoint_specific_kwargs_survive_confirmation_artifact_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "endpoint_failure.py").write_text(
        """
def transform(frame, *, engine):
    if engine == "polars":
        return frame.append_column("candidate_only", frame.column(0))
    return frame
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"x": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="endpoint-replay",
        reference=CallableSpec(
            target="endpoint_failure:transform", adapter="arrow", workdir=tmp_path
        ),
        candidate=CallableSpec(
            target="endpoint_failure:transform", adapter="arrow", workdir=tmp_path
        ),
        fixture=fixture,
        reference_kwargs={"engine": "pandas"},
        candidate_kwargs={"engine": "polars"},
        generation=GenerationConfig(
            max_examples=1,
            adversarial_examples=False,
            search=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )

    result = engine.run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    failure = result.cases[0].failures[0]
    assert result.status is Status.FAILED
    assert failure.finding_signature is not None
    assert failure.artifact is not None
    replay_contract = json.loads((failure.artifact / "replay.json").read_text(encoding="utf-8"))
    assert replay_contract["case"]["reference_kwargs"] == {"engine": "pandas"}
    assert replay_contract["case"]["candidate_kwargs"] == {"engine": "polars"}

    replayed = replay_artifact(failure.artifact)

    assert replayed.status is Status.FAILED
    assert replayed.cases[0].failures[0].finding_signature == failure.finding_signature


def test_importable_live_failure_is_confirmed_in_a_fresh_process(tmp_path: Path) -> None:
    global _live_stateful_calls
    _live_stateful_calls = 0

    result = _run(
        tmp_path,
        corrupt_after_live_warmup,
        generation=GenerationConfig(max_examples=20, derandomize=True),
    )

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert case.failures[0].finding_signature is None
    assert "not reproducible" in case.failures[0].mismatches[0].message


def test_exact_replay_observes_saved_input_once_per_side(tmp_path: Path) -> None:
    calls = {"reference": 0, "candidate": 0}

    def runner(label: str, value: pa.Table) -> Observation:
        calls[label] += 1
        output = value if label == "reference" else pa.table({"x": [2]})
        return Observation(
            outcome=ExecutionOutcome.RETURNED,
            table=output,
            metrics=RunMetrics(duration_seconds=0),
        )

    result = engine._campaign(
        name="exact",
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="int64", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        fixture=pa.table({"x": [1]}),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=100, max_findings=10),
        performance_config=PerformanceConfig(enabled=False),
        artifact_store=ArtifactStore(tmp_path),
        reference_runner=lambda value: runner("reference", value),
        candidate_runner=lambda value: runner("candidate", value),
        artifact_case="exact",
        exact_only=True,
    )

    assert result.status is Status.FAILED
    assert calls == {"reference": 1, "candidate": 1}
    assert result.examples_run == 1


def test_generated_infrastructure_error_stops_without_shrinking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def execute(
        function: Any,
        _table: Any,
        *,
        adapter: str = "auto",
        pandas_input: PandasInput = "arrow",
        record_distributions: Sequence[str] = (),
    ) -> Observation:
        nonlocal calls
        assert adapter == "auto"
        assert pandas_input == "arrow"
        assert not record_distributions
        calls += 1
        return Observation(
            outcome=(
                ExecutionOutcome.RETURNED if function is identity else ExecutionOutcome.CRASHED
            ),
            metrics=RunMetrics(duration_seconds=0),
            value=None,
            has_value=True,
        )

    monkeypatch.setattr(engine, "execute_callable_current", execute)
    result = _run(
        tmp_path,
        corrupt_seven,
        generation=GenerationConfig(max_examples=500, adversarial_examples=False),
    )
    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.generated_examples == 1
    assert case.failures[0].source == "generated:error"
    assert calls == 2


def test_matching_unsupported_live_returns_are_an_error(tmp_path: Path) -> None:
    result = run_live(
        return_unsupported,
        return_unsupported,
        fixture=None,
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
    )

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert case.failures[0].finding_signature is None
    assert all(
        "could not be executed" in mismatch.message for mismatch in case.failures[0].mismatches
    )


def test_matching_unsupported_configured_returns_are_an_error(tmp_path: Path) -> None:
    (tmp_path / "unsupported_transforms.py").write_text(
        "def unsupported(_frame):\n    return object()\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="unsupported_transforms:unsupported",
        adapter="pandas",
        workdir=tmp_path,
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="unsupported-return",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(max_examples=1, adversarial_examples=False),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert case.failures[0].finding_signature is None
    assert len(case.failures[0].mismatches) == 2


def test_matching_failed_tabular_canonicalization_is_an_error_live(tmp_path: Path) -> None:
    result = run_live(
        return_complex_dataframe,
        return_complex_dataframe,
        fixture=pd.DataFrame({"x": [1]}),
        schema=None,
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
    )

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert case.failures[0].finding_signature is None
    assert [mismatch.path for mismatch in case.failures[0].mismatches] == [
        "$reference",
        "$candidate",
    ]


def test_matching_failed_tabular_canonicalization_is_an_error_configured(
    tmp_path: Path,
) -> None:
    (tmp_path / "complex_output.py").write_text(
        "import pandas as pd\n"
        "def transform(_frame):\n"
        "    return pd.DataFrame({'complex': [1 + 2j]})\n",
        encoding="utf-8",
    )
    spec = CallableSpec(target="complex_output:transform", adapter="pandas", workdir=tmp_path)
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="complex-output",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(max_examples=1, adversarial_examples=False),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert case.failures[0].finding_signature is None
    assert [mismatch.path for mismatch in case.failures[0].mismatches] == [
        "$reference",
        "$candidate",
    ]


def test_matching_failed_polars_input_materialization_is_an_error_live(
    tmp_path: Path,
) -> None:
    result = run_live(
        unannotated_polars_identity,
        unannotated_polars_identity,
        fixture=_dense_union_table(),
        schema=FrameSchema(
            columns=[ColumnSchema(name="mixed", dtype="object")],
            min_rows=1,
            max_rows=3,
        ),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="polars",
        candidate_adapter="polars",
    )

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert [mismatch.path for mismatch in case.failures[0].mismatches] == [
        "$reference",
        "$candidate",
    ]


def test_matching_failed_polars_input_materialization_is_an_error_configured(
    tmp_path: Path,
) -> None:
    (tmp_path / "polars_input.py").write_text(
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "dense-union.arrow"
    table = _dense_union_table()
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    spec = CallableSpec(target="polars_input:transform", adapter="polars", workdir=tmp_path)
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="polars-input",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                fixture=fixture,
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="mixed", dtype="object")],
                    min_rows=1,
                    max_rows=3,
                ),
                generation=GenerationConfig(max_examples=1, adversarial_examples=False),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert [mismatch.path for mismatch in case.failures[0].mismatches] == [
        "$reference",
        "$candidate",
    ]


def test_matching_import_time_system_exit_is_an_error_configured(tmp_path: Path) -> None:
    (tmp_path / "exit_during_import.py").write_text(
        'raise SystemExit("private import detail")\n',
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="exit_during_import:transform",
        adapter="pandas",
        workdir=tmp_path,
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="import-exit",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(max_examples=1, adversarial_examples=False),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert [mismatch.path for mismatch in case.failures[0].mismatches] == [
        "$reference",
        "$candidate",
    ]
    assert all(
        "private import detail" not in mismatch.message for mismatch in case.failures[0].mismatches
    )


def test_json_mapping_key_coercion_cannot_hide_a_live_difference(tmp_path: Path) -> None:
    result = run_live(
        return_integer_mapping_key,
        return_string_mapping_key,
        fixture=None,
        schema=FrameSchema(
            columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
            min_rows=1,
            max_rows=1,
        ),
        comparison=ComparisonPolicy(),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
        artifact_dir=tmp_path,
        reference_adapter="pandas",
        candidate_adapter="pandas",
    )

    case = result.cases[0]
    assert result.status is Status.ERROR
    assert case.findings_discovered == 0
    assert [mismatch.path for mismatch in case.failures[0].mismatches] == ["$reference"]


def test_json_mapping_key_coercion_cannot_hide_a_configured_difference(tmp_path: Path) -> None:
    (tmp_path / "mapping_key_transforms.py").write_text(
        """
def integer_key(_frame):
    return {1: "same"}

def string_key(_frame):
    return {"1": "same"}
""",
        encoding="utf-8",
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="mapping-key-coercion",
                reference=CallableSpec(
                    target="mapping_key_transforms:integer_key",
                    adapter="pandas",
                    workdir=tmp_path,
                ),
                candidate=CallableSpec(
                    target="mapping_key_transforms:string_key",
                    adapter="pandas",
                    workdir=tmp_path,
                ),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(max_examples=1, adversarial_examples=False),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    assert result.status is Status.ERROR
    assert result.cases[0].findings_discovered == 0
    assert [mismatch.path for mismatch in result.cases[0].failures[0].mismatches] == ["$reference"]


def test_vanished_shrunk_witness_is_reported_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def find(_schema, classifier, _excluded, _generation):
        table = pa.table({"x": [7]})
        assert classifier(table) is not None
        return type("Found", (), {"table": pa.table({"x": [7]}), "source": "generated:shrunk"})()

    monkeypatch.setattr(engine, "find_unseen_counterexample", find)
    calls = 0

    def observe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        observation = Observation(
            outcome=ExecutionOutcome.RETURNED,
            table=pa.table({"x": [7]}),
            metrics=RunMetrics(duration_seconds=0),
        )
        if calls == 1:
            return (
                observation,
                observation,
                [Mismatch(kind=MismatchKind.VALUE, message="transient")],
                Status.FAILED,
            )
        return observation, observation, [], Status.PASSED

    monkeypatch.setattr(engine, "_observe_pair", observe)
    result = _run(
        tmp_path,
        corrupt_seven,
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
    )

    assert result.status is Status.ERROR
    failure = result.cases[0].failures[0]
    assert failure.status is Status.ERROR
    assert "not reproducible" in failure.mismatches[0].message


def test_configured_campaign_reuses_two_distinct_worker_sessions(tmp_path: Path) -> None:
    (tmp_path / "session_transforms.py").write_text(
        """
import os

_calls = 0

def record_process(frame, log_path):
    global _calls
    _calls += 1
    with open(log_path, 'a', encoding='utf-8') as stream:
        stream.write(f'{os.getpid()}:{_calls}\\n')
    return frame
""",
        encoding="utf-8",
    )
    process_log = tmp_path / "processes.txt"
    spec = CallableSpec(
        target="session_transforms:record_process", adapter="pandas", workdir=tmp_path
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="persistent-sessions",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    max_rows=2,
                ),
                static_args=[str(process_log)],
                generation=GenerationConfig(
                    max_examples=3, adversarial_examples=False, derandomize=True
                ),
                performance=PerformanceConfig(enabled=False),
                timeout_seconds=5,
            )
        ],
    )

    result = engine.run_suite(config)

    assert result.status is Status.PASSED
    calls = [line.split(":") for line in process_log.read_text(encoding="utf-8").splitlines()]
    by_process: dict[str, list[int]] = {}
    for pid, count in calls:
        by_process.setdefault(pid, []).append(int(count))
    assert len(by_process) == 2
    assert all(sorted(counts) == list(range(1, len(counts) + 1)) for counts in by_process.values())
    assert all(len(counts) >= 3 for counts in by_process.values())


def test_configured_campaign_records_each_worker_distribution_version(tmp_path: Path) -> None:
    (tmp_path / "provenance_transform.py").write_text(
        "def identity(frame):\n    return frame.copy()\n", encoding="utf-8"
    )
    environments: list[Path] = []
    for side, version in (("reference", "1.0"), ("candidate", "2.0")):
        root = tmp_path / side
        metadata = root / f"demo_target-{version}.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: demo-target\nVersion: {version}\n",
            encoding="utf-8",
        )
        environments.append(root)
    reference_root, candidate_root = environments
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="separate-runtime-provenance",
                reference=CallableSpec(
                    target="provenance_transform:identity",
                    adapter="pandas",
                    workdir=tmp_path,
                    environment={"PYTHONPATH": str(reference_root)},
                    record_distributions=["demo-target"],
                ),
                candidate=CallableSpec(
                    target="provenance_transform:identity",
                    adapter="pandas",
                    workdir=tmp_path,
                    environment={"PYTHONPATH": str(candidate_root)},
                    record_distributions=["demo-target"],
                ),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(adversarial_examples=False, max_examples=1),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    provenance = result.cases[0].provenance
    assert provenance is not None
    assert provenance.reference is not None
    assert provenance.candidate is not None
    reference_versions = {item.name: item.version for item in provenance.reference.distributions}
    candidate_versions = {item.name: item.version for item in provenance.candidate.distributions}
    assert reference_versions["demo-target"] == "1.0"
    assert candidate_versions["demo-target"] == "2.0"


def test_configured_campaign_rejects_matching_runtime_failures_before_import(
    tmp_path: Path,
) -> None:
    imported = tmp_path / "configured-target-imported.txt"
    (tmp_path / "configured_contract_target.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(imported)!r}).write_text('imported', encoding='utf-8')\n"
        "def identity(frame):\n"
        "    return frame\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="configured_contract_target:identity",
        adapter="arrow",
        workdir=tmp_path,
        required_distributions={"definitely-missing-parity-contract": ">=1"},
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="runtime-contract",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(adversarial_examples=False, max_examples=1),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = engine.run_suite(config)

    assert result.status is Status.ERROR
    case = result.cases[0]
    assert case.status is Status.ERROR
    assert case.examples_run == 0
    assert case.failures[0].source == "runtime:preflight"
    assert len(case.failures[0].mismatches) == 2
    assert all(
        "distributions.definitely-missing-parity-contract.missing" in mismatch.message
        for mismatch in case.failures[0].mismatches
    )
    assert str(tmp_path) not in json.dumps(case.model_dump(mode="json"))
    assert "configured_contract_target" not in json.dumps(case.model_dump(mode="json"))
    assert imported.exists() is False


def test_configured_campaign_requires_worker_parity_match_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported = tmp_path / "parity-mismatch-target-imported.txt"
    (tmp_path / "parity_mismatch_target.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(imported)!r}).write_text('imported', encoding='utf-8')\n"
        "def identity(frame):\n"
        "    return frame\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="parity_mismatch_target:identity",
        adapter="arrow",
        workdir=tmp_path,
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="parity-version-contract",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    columns=[ColumnSchema(name="x", dtype="integer", nullable=False)],
                    min_rows=1,
                    max_rows=1,
                ),
                generation=GenerationConfig(adversarial_examples=False, max_examples=1),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )
    monkeypatch.setattr("parity.execution.__version__", "999.0.0")

    result = engine.run_suite(config)

    assert result.status is Status.ERROR
    assert result.cases[0].examples_run == 0
    assert all(
        "parity_version" in mismatch.message for mismatch in result.cases[0].failures[0].mismatches
    )
    assert imported.exists() is False


def test_artifact_replay_resolves_import_root_from_invocation_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    transform_root = tmp_path / "transforms"
    transform_root.mkdir()
    (transform_root / "replay_transform.py").write_text(
        "def identity(frame):\n    return frame.copy()\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="replay-workdir",
        reference=CallableSpec(
            target="replay_transform:identity", adapter="pandas", workdir=transform_root
        ),
        candidate=CallableSpec(
            target="replay_transform:identity", adapter="pandas", workdir=transform_root
        ),
        fixture=tmp_path / "original.parquet",
        generation=GenerationConfig(adversarial_examples=False, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        case,
        pa.table({"x": [1, 2]}),
        ExampleResult(source="test", status=Status.FAILED),
        runtime_provenance=CaseProvenance(
            reference=collect_runtime_provenance(),
            candidate=collect_runtime_provenance(),
        ),
        config_sha256="a" * 64,
    )

    result = replay_artifact(artifact)

    assert result.status is Status.PASSED
    assert result.cases[0].provenance is not None
    assert result.cases[0].provenance.verification == "verified"
    assert not (artifact / "replay-output").exists()


def test_configured_named_bundle_failure_replays_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "join_transforms.py").write_text(
        "def reference(left, right):\n"
        "    return left.merge(right, on='key', how='left', sort=True)\n"
        "def candidate(left, right):\n"
        "    result = left.merge(right, on='key', how='left', sort=True)\n"
        "    result['value'] = result['value'].fillna(-1)\n"
        "    return result\n",
        encoding="utf-8",
    )
    left_path = tmp_path / "left.arrow"
    right_path = tmp_path / "right.arrow"
    for path, table in (
        (left_path, pa.table({"key": [1, 2]})),
        (right_path, pa.table({"key": [1], "value": [10]})),
    ):
        with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="named-bundle-replay",
        reference=CallableSpec(
            target="join_transforms:reference", adapter="pandas", workdir=tmp_path
        ),
        candidate=CallableSpec(
            target="join_transforms:candidate", adapter="pandas", workdir=tmp_path
        ),
        input_bundle=InputBundle(
            inputs={
                "left": InputSpec(fixture=left_path),
                "right": InputSpec(fixture=right_path),
            }
        ),
        generation=GenerationConfig(
            max_examples=1, adversarial_examples=False, suppress_too_slow=False
        ),
        performance=PerformanceConfig(enabled=False),
    )
    result = engine.run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))
    artifact = result.cases[0].failures[0].artifact

    assert result.status is Status.FAILED
    assert artifact is not None
    replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    assert replay["version"] == 1
    assert [item["name"] for item in replay["inputs"]] == ["left", "right"]

    replayed = replay_artifact(artifact)
    assert replayed.status is Status.FAILED
    assert replayed.cases[0].failures[0].artifact == artifact


def test_configured_worker_and_replay_detect_large_integer_precision_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "precision_transforms.py").write_text(
        "def reference(_frame):\n"
        "    return {'value': 9007199254740992}\n"
        "def candidate(_frame):\n"
        "    return {'value': 9007199254740993}\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"x": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="numeric-precision-replay",
        reference=CallableSpec(
            target="precision_transforms:reference", adapter="pandas", workdir=tmp_path
        ),
        candidate=CallableSpec(
            target="precision_transforms:candidate", adapter="pandas", workdir=tmp_path
        ),
        fixture=fixture,
        comparison=ComparisonPolicy(rtol=0, atol=0),
        generation=GenerationConfig(max_examples=1, adversarial_examples=False),
        performance=PerformanceConfig(enabled=False),
    )

    result = engine.run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))
    failure = result.cases[0].failures[0]

    assert result.status is Status.FAILED
    assert failure.mismatches[0].path == "$['value']"
    assert failure.artifact is not None
    replayed = replay_artifact(failure.artifact)
    assert replayed.status is Status.FAILED
    assert replayed.cases[0].failures[0].mismatches[0].path == "$['value']"


def test_positional_bundle_replay_restores_hash_bound_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="positional-order",
        reference=CallableSpec(target="test_engine:identity", adapter="pandas"),
        candidate=CallableSpec(target="test_engine:identity", adapter="pandas"),
        input_bundle=InputBundle(
            binding="positional",
            inputs={
                "zebra": InputSpec(fixture=tmp_path / "zebra.arrow"),
                "alpha": InputSpec(fixture=tmp_path / "alpha.arrow"),
            },
        ),
        generation=GenerationConfig(adversarial_examples=False, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        case,
        {
            "zebra": pa.table({"x": [1]}),
            "alpha": pa.table({"x": [2]}),
        },
        ExampleResult(source="test", status=Status.FAILED),
        runtime_provenance=CaseProvenance(
            reference=collect_runtime_provenance(),
            candidate=collect_runtime_provenance(),
        ),
        config_sha256="a" * 64,
    )
    replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    assert list(replay["case"]["input_bundle"]["inputs"]) == ["alpha", "zebra"]
    assert [item["name"] for item in replay["inputs"]] == ["zebra", "alpha"]

    observed: dict[str, tuple[str, ...]] = {}

    class CapturedCase(Exception):
        pass

    def capture_case(replay_case: CaseConfig, *_args: Any, **_kwargs: Any) -> None:
        assert replay_case.input_bundle is not None
        observed["names"] = tuple(replay_case.input_bundle.inputs)
        raise CapturedCase

    monkeypatch.setattr(engine, "_configured_case", capture_case)
    with pytest.raises(CapturedCase):
        replay_artifact(artifact)

    assert observed["names"] == ("zebra", "alpha")


def test_replay_runtime_drift_blocks_both_callables_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "invoked.txt"
    module = tmp_path / "drift_transform.py"
    module.write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "MARKER.write_text('imported', encoding='utf-8')\n"
        "def touch(frame):\n"
        "    MARKER.write_text('invoked', encoding='utf-8')\n"
        "    return frame.copy()\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    runtime = collect_runtime_provenance()
    case = CaseConfig(
        name="runtime-drift",
        reference=CallableSpec(target="drift_transform:touch", adapter="pandas", workdir=tmp_path),
        candidate=CallableSpec(target="drift_transform:touch", adapter="pandas", workdir=tmp_path),
        fixture=tmp_path / "unused.arrow",
        generation=GenerationConfig(adversarial_examples=False, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        case,
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="b" * 64,
    )
    replay_path = artifact / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["expected_runtime"]["reference"]["python_version"] = "0.0.0"
    replay_path.write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replay_content = replay_path.read_bytes()
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["replay.json"] = {
        "sha256": hashlib.sha256(replay_content).hexdigest(),
        "bytes": len(replay_content),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = replay_artifact(artifact)

    assert result.status is Status.ERROR
    assert result.cases[0].provenance is not None
    assert result.cases[0].provenance.verification == "drifted"
    assert result.cases[0].failures[0].source == "replay:provenance"
    assert not marker.exists()


def test_replay_keeps_verified_provenance_when_callable_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "crash_transform.py").write_text(
        "import os\n"
        "def identity(frame):\n    return frame.copy()\n"
        "def crash(_frame):\n    os._exit(9)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    runtime = collect_runtime_provenance()
    case = CaseConfig(
        name="verified-crash",
        reference=CallableSpec(
            target="crash_transform:identity", adapter="pandas", workdir=tmp_path
        ),
        candidate=CallableSpec(target="crash_transform:crash", adapter="pandas", workdir=tmp_path),
        fixture=tmp_path / "unused.arrow",
        generation=GenerationConfig(adversarial_examples=False, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        case,
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.ERROR),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="c" * 64,
    )

    result = replay_artifact(artifact)

    assert result.status is Status.ERROR
    assert result.cases[0].provenance is not None
    assert result.cases[0].provenance.verification == "verified"
    assert "runtime provenance drifted" not in render_terminal(result)


@pytest.mark.parametrize("unsafe", ["../outside", "nested/../../outside"])
def test_replay_rejects_workdir_traversal(tmp_path: Path, unsafe: str) -> None:
    case_data = {
        "reference": {"workdir": unsafe},
        "candidate": {"workdir": None},
    }

    with pytest.raises(engine.ReplayError, match="stay inside"):
        engine._resolve_replay_paths(case_data, tmp_path)


def test_replay_rejects_workdir_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    case_data = {
        "reference": {"workdir": "escape"},
        "candidate": {"workdir": None},
    }

    with pytest.raises(engine.ReplayError, match="stay inside"):
        engine._resolve_replay_paths(case_data, tmp_path)


def test_replay_rejects_python_path_escape(tmp_path: Path) -> None:
    case_data = {
        "reference": {"workdir": None, "python": "../venv/bin/python"},
        "candidate": {"workdir": None, "python": None},
    }

    with pytest.raises(engine.ReplayError, match="python paths must stay inside"):
        engine._resolve_replay_paths(case_data, tmp_path)


def test_replay_preserves_project_venv_symlink_to_system_python(tmp_path: Path) -> None:
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    case_data = {
        "reference": {"workdir": None, "python": ".venv/bin/python"},
        "candidate": {"workdir": None, "python": ".venv/bin/python"},
    }

    engine._resolve_replay_paths(case_data, tmp_path)

    assert case_data["reference"]["python"] == interpreter
    assert case_data["candidate"]["python"] == interpreter
    assert case_data["reference"]["python"].resolve() == Path(sys.executable).resolve()


def test_replay_rejects_missing_project_python_path(tmp_path: Path) -> None:
    case_data = {
        "reference": {"workdir": None, "python": ".venv/bin/python"},
        "candidate": {"workdir": None, "python": None},
    }

    with pytest.raises(engine.ReplayError, match="python path must be an existing file"):
        engine._resolve_replay_paths(case_data, tmp_path)


def test_replay_rejects_python_parent_directory_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-venv"
    interpreter = outside / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    (tmp_path / ".venv").symlink_to(outside, target_is_directory=True)
    case_data = {
        "reference": {"workdir": None, "python": ".venv/bin/python"},
        "candidate": {"workdir": None, "python": None},
    }

    with pytest.raises(engine.ReplayError, match="parent directories must stay inside"):
        engine._resolve_replay_paths(case_data, tmp_path)


def test_artifact_replay_runs_through_project_virtualenv_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    virtualenv = tmp_path / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(virtualenv)],
        check=True,
    )
    interpreter = virtualenv / "bin" / "python"
    assert interpreter.is_symlink()
    source_site_packages = (
        Path(sys.executable).parent.parent
        / "lib"
        / (f"python{sys.version_info.major}.{sys.version_info.minor}")
        / "site-packages"
    )
    monkeypatch.setenv("PYTHONPATH", str(source_site_packages))
    (tmp_path / "replay_transform.py").write_text(
        "def identity(frame):\n    return frame\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="replay_transform:identity",
        adapter="arrow",
        python=interpreter,
        workdir=tmp_path,
        environment={"PYTHONPATH": str(source_site_packages)},
    )
    with IsolatedExecutionSession(spec) as session:
        runtime_observation = session.inspect_runtime()
    assert runtime_observation.outcome is ExecutionOutcome.RETURNED
    assert runtime_observation.runtime is not None
    runtime = runtime_observation.runtime
    case = CaseConfig(
        name="venv-replay",
        reference=spec,
        candidate=spec.model_copy(deep=True),
        fixture=tmp_path / "unused.arrow",
        performance=PerformanceConfig(enabled=False),
    )
    monkeypatch.chdir(tmp_path)
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        case,
        pa.table({"id": [1, 2]}),
        ExampleResult(source="test", status=Status.FAILED),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="e" * 64,
    )

    result = replay_artifact(artifact)

    assert result.status is Status.PASSED


def test_configured_artifact_fingerprint_distinguishes_virtualenv_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fingerprint_transform.py").write_text(
        "def reference(frame):\n    return frame\n"
        "def candidate(frame):\n    return frame.append_column('extra', frame.column(0))\n",
        encoding="utf-8",
    )
    for name in (".venv-old", ".venv-new"):
        interpreter = tmp_path / name / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
    source_site_packages = (
        Path(sys.executable).parent.parent
        / "lib"
        / (f"python{sys.version_info.major}.{sys.version_info.minor}")
        / "site-packages"
    )
    monkeypatch.setenv("PYTHONPATH", str(source_site_packages))
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"id": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    monkeypatch.chdir(tmp_path)

    def run(reference_python: Path, candidate_python: Path) -> tuple[str, Path]:
        case = CaseConfig(
            name="versions",
            reference=CallableSpec(
                target="fingerprint_transform:reference",
                adapter="arrow",
                python=reference_python,
                workdir=tmp_path,
                environment={"PYTHONPATH": str(source_site_packages)},
            ),
            candidate=CallableSpec(
                target="fingerprint_transform:candidate",
                adapter="arrow",
                python=candidate_python,
                workdir=tmp_path,
                environment={"PYTHONPATH": str(source_site_packages)},
            ),
            fixture=fixture,
            generation=GenerationConfig(
                max_examples=1, adversarial_examples=False, stability_repeats=1
            ),
            performance=PerformanceConfig(enabled=False),
        )
        result = engine.run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))
        artifact = result.cases[0].failures[0].artifact
        assert artifact is not None
        replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
        return replay["config_sha256"], artifact

    old = tmp_path / ".venv-old" / "bin" / "python"
    new = tmp_path / ".venv-new" / "bin" / "python"
    distinct_hash, artifact = run(old, new)
    same_hash, _ = run(old, old)

    assert distinct_hash != same_hash
    replayed = replay_artifact(artifact)
    assert replayed.provenance is not None
    assert replayed.provenance.config_sha256 == distinct_hash


def test_run_suite_hash_uses_loaded_config_base_for_virtualenv_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("old", "new"):
        interpreter = tmp_path / name / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
    config_path = tmp_path / "parity.toml"
    config_path.write_text(
        """
version = 1

[[cases]]
name = "versions"
fixture = "fixture.arrow"

[cases.reference]
target = "project:reference"
python = "old/bin/python"

[cases.candidate]
target = "project:candidate"
python = "new/bin/python"
""",
        encoding="utf-8",
    )
    captured: list[str] = []

    def configured_case(_case: CaseConfig, _store: ArtifactStore, **kwargs: Any) -> CaseResult:
        captured.append(kwargs["config_sha256"])
        return CaseResult(name="versions", status=Status.PASSED)

    monkeypatch.setattr(engine, "_configured_case", configured_case)
    config = load_config(config_path)
    first = engine.run_suite(config)
    config.cases[0].candidate.python = config.cases[0].reference.python
    second = engine.run_suite(config)

    assert captured[0] != captured[1]
    assert first.provenance is not None
    assert second.provenance is not None
    assert first.provenance.config_sha256 == captured[0]
    assert second.provenance.config_sha256 == captured[1]


def test_replay_manifest_must_bind_every_consumed_file(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        "manifest",
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
    )
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"]["replay.json"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(engine.ReplayError, match="missing required file"):
        replay_artifact(artifact)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", True, "unsupported replay contract"),
        ("version", 1.0, "unsupported replay contract"),
        ("version", 2, "unsupported replay contract"),
        ("expected_runtime", None, "runtime provenance is missing or invalid"),
        ("config_sha256", None, "configuration fingerprint is missing or invalid"),
    ],
)
def test_replay_contract_is_current_and_fail_closed(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    runtime = collect_runtime_provenance()
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        "bound-contract",
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="d" * 64,
    )
    replay_path = artifact / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay[field] = value
    replay_path.write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replay_content = replay_path.read_bytes()
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["replay.json"] = {
        "sha256": hashlib.sha256(replay_content).hexdigest(),
        "bytes": len(replay_content),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(engine.ReplayError, match=message):
        replay_artifact(artifact)


def test_inspection_only_artifact_cannot_execute_automatically(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        "inspection-only",
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
    )
    replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    assert "command" not in replay

    with pytest.raises(engine.ReplayError, match="runtime provenance is missing or invalid"):
        replay_artifact(artifact)


@pytest.mark.parametrize("unsupported_version", [True, 1.0, 2])
def test_replay_rejects_unsupported_manifest_version(
    tmp_path: Path, unsupported_version: object
) -> None:
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        "manifest-version",
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
    )
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = unsupported_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(engine.ReplayError, match="unsupported artifact manifest"):
        replay_artifact(artifact)


def test_replay_manifest_rejects_symlinked_external_file(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        "manifest-symlink",
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
    )
    input_path = artifact / "input.arrow"
    external = tmp_path / "external.arrow"
    external.write_bytes(input_path.read_bytes())
    input_path.unlink()
    try:
        input_path.symlink_to(external)
    except OSError:
        pytest.skip("filesystem does not permit symlink creation")

    with pytest.raises(engine.ReplayError, match="regular contained file"):
        engine._verify_manifest(artifact)


@pytest.mark.parametrize(
    ("field", "argument"),
    [
        ("static_args", "/private/customer.csv"),
        ("static_args", "API_TOKEN=secret"),
        ("reference_kwargs", "API_TOKEN=reference-secret"),
        ("candidate_kwargs", "/private/candidate.csv"),
    ],
)
def test_replay_rejects_sanitized_static_arguments(
    tmp_path: Path, field: str, argument: str
) -> None:
    arguments: dict[str, object] = (
        {field: [argument]} if field == "static_args" else {field: {"option": argument}}
    )
    monkey_case = CaseConfig(
        name="sanitized-argument",
        reference=CallableSpec(target="test_engine:identity", adapter="pandas"),
        candidate=CallableSpec(target="test_engine:identity", adapter="pandas"),
        fixture=tmp_path / "unused.parquet",
        **arguments,
        generation=GenerationConfig(adversarial_examples=False, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        monkey_case,
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
        runtime_provenance=CaseProvenance(
            reference=collect_runtime_provenance(),
            candidate=collect_runtime_provenance(),
        ),
        config_sha256="f" * 64,
    )
    replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    assert "command" not in replay

    with pytest.raises(engine.ReplayError, match="redacted static arguments"):
        replay_artifact(artifact)
