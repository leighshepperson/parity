from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

import parity.engine as engine
from parity.artifacts import ArtifactStore
from parity.engine import replay_artifact, run_live
from parity.execution import ExecutionOutcome, Observation
from parity.models import (
    CallableSpec,
    CaseConfig,
    ColumnSchema,
    ComparisonPolicy,
    ExampleResult,
    FrameSchema,
    GenerationConfig,
    Mismatch,
    MismatchKind,
    ParityConfig,
    PerformanceConfig,
    RunMetrics,
    Status,
)


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


def unannotated_polars_identity(frame):
    assert isinstance(frame, pl.DataFrame)
    return frame


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
    )
    artifact = result.cases[0].failures[0].artifact

    assert artifact is not None
    replay_contract = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))["case"]
    assert replay_contract["reference"]["adapter"] == "pandas"
    assert replay_contract["candidate"]["adapter"] == "pandas"
    assert replay_contract["comparison"]["dtype"] == "strict"
    assert replay_artifact(artifact).status is Status.FAILED


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
    replay_contract = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))["case"]
    assert replay_contract["candidate"] is None
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


def test_generated_infrastructure_error_stops_without_shrinking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def execute(function: Any, _table: Any, *, adapter: str = "auto") -> Observation:
        nonlocal calls
        assert adapter == "auto"
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


def test_vanished_shrunk_witness_is_reported_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def find(_schema, predicate, _generation):
        table = pa.table({"x": [7]})
        assert predicate(table)
        return type("Found", (), {"table": pa.table({"x": [7]}), "source": "generated:shrunk"})()

    monkeypatch.setattr(engine, "find_counterexample", find)
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
    )

    result = replay_artifact(artifact)

    assert result.status is Status.PASSED
    assert not (artifact / "replay-output").exists()


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


@pytest.mark.parametrize("argument", ["/private/customer.csv", "API_TOKEN=secret"])
def test_replay_rejects_sanitized_static_arguments(tmp_path: Path, argument: str) -> None:
    monkey_case = CaseConfig(
        name="sanitized-argument",
        reference=CallableSpec(target="test_engine:identity", adapter="pandas"),
        candidate=CallableSpec(target="test_engine:identity", adapter="pandas"),
        fixture=tmp_path / "unused.parquet",
        static_args=[argument],
        generation=GenerationConfig(adversarial_examples=False, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        monkey_case,
        pa.table({"x": [1]}),
        ExampleResult(source="test", status=Status.FAILED),
    )

    with pytest.raises(engine.ReplayError, match="redacted static arguments"):
        replay_artifact(artifact)
