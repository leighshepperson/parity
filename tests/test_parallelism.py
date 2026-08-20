from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import parity.engine as engine
from parity.execution import _isolated_environment
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseResult,
    ColumnSchema,
    FrameSchema,
    GenerationConfig,
    ParityConfig,
    PerformanceConfig,
    Status,
)


def _case(name: str, seed: int) -> CaseConfig:
    return CaseConfig(
        name=name,
        reference=CallableSpec(target="project:reference"),
        candidate=CallableSpec(target="project:candidate"),
        input_schema=FrameSchema(columns=[ColumnSchema(name="x", dtype="integer")]),
        generation=GenerationConfig(seed=seed, max_examples=1),
        performance=PerformanceConfig(enabled=False),
    )


def test_cases_run_concurrently_but_results_and_seeds_keep_config_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["slow", "fast", "middle"]
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        jobs=3,
        cases=[_case(name, seed) for name, seed in zip(names, (101, 202, 303), strict=True)],
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    observed_seeds: dict[str, int | None] = {}

    def configured_case(case: CaseConfig, *_args: Any, **_kwargs: Any) -> CaseResult:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            observed_seeds[case.name] = case.generation.seed
        time.sleep({"slow": 0.06, "fast": 0.01, "middle": 0.03}[case.name])
        with lock:
            active -= 1
        return CaseResult(name=case.name, status=Status.PASSED)

    monkeypatch.setattr(engine, "_configured_case", configured_case)

    result = engine.run_suite(config)

    assert maximum_active == 3
    assert [case.name for case in result.cases] == names
    assert observed_seeds == {"slow": 101, "fast": 202, "middle": 303}


def test_parallel_selected_cases_preserve_relative_configuration_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        jobs=2,
        cases=[_case("first", 1), _case("second", 2), _case("third", 3)],
    )
    monkeypatch.setattr(
        engine,
        "_configured_case",
        lambda case, *_args, **_kwargs: CaseResult(name=case.name, status=Status.PASSED),
    )

    result = engine.run_suite(config, selected_cases={"third", "first"})

    assert [case.name for case in result.cases] == ["first", "third"]


def test_parallel_fail_fast_is_rejected_instead_of_racing() -> None:
    with pytest.raises(ValidationError, match="fail_fast=true"):
        ParityConfig(jobs=2, fail_fast=True, cases=[_case("one", 1)])


def test_parallel_enforced_performance_gate_is_rejected_to_avoid_contention() -> None:
    case = _case("benchmark", 1)
    case.performance = PerformanceConfig(
        enabled=True,
        repeats=5,
        fail_on_regression=True,
    )

    with pytest.raises(ValidationError, match=r"performance gates require jobs=1"):
        ParityConfig(jobs=2, cases=[case])


def test_native_thread_limit_is_opt_in_and_endpoint_environment_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "64")
    unlimited = _isolated_environment(CallableSpec(target="project:reference"))
    limited = _isolated_environment(
        CallableSpec(
            target="project:reference",
            native_threads=2,
            environment={"MKL_NUM_THREADS": "3"},
        )
    )

    assert unlimited["OPENBLAS_NUM_THREADS"] == "64"
    assert limited["OPENBLAS_NUM_THREADS"] == "2"
    assert limited["OMP_NUM_THREADS"] == "2"
    assert limited["MKL_NUM_THREADS"] == "3"


def test_suite_native_thread_limit_is_applied_without_mutating_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _case("limited", 11)
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        jobs=1,
        native_threads=2,
        cases=[original],
    )
    captured: list[tuple[int | None, int | None]] = []

    def configured_case(case: CaseConfig, *_args: Any, **_kwargs: Any) -> CaseResult:
        captured.append((case.reference.native_threads, case.candidate.native_threads))
        return CaseResult(name=case.name, status=Status.PASSED)

    monkeypatch.setattr(engine, "_configured_case", configured_case)

    engine.run_suite(config)

    assert captured == [(2, 2)]
    assert original.reference.native_threads is None
    assert original.candidate.native_threads is None


@pytest.mark.integration
def test_parallel_cases_run_with_real_isolated_worker_pairs(tmp_path: Path) -> None:
    (tmp_path / "parallel_project.py").write_text(
        "def identity(frame):\n"
        "    return frame.copy()\n"
        "\n"
        "def corrupt(frame):\n"
        "    result = frame.copy()\n"
        "    result['x'] = result['x'] + 1\n"
        "    return result\n",
        encoding="utf-8",
    )
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="x",
                dtype="integer",
                nullable=False,
                minimum=0,
                maximum=5,
            )
        ],
        min_rows=1,
        max_rows=2,
    )
    cases = [
        CaseConfig(
            name=name,
            reference=CallableSpec(
                target="parallel_project:identity", adapter="pandas", workdir=tmp_path
            ),
            candidate=CallableSpec(
                target="parallel_project:corrupt", adapter="pandas", workdir=tmp_path
            ),
            input_schema=schema,
            generation=GenerationConfig(
                max_examples=3,
                seed=seed,
                adversarial_examples=False,
                stability_repeats=1,
            ),
            performance=PerformanceConfig(enabled=False),
        )
        for name, seed in (("first", 7), ("second", 8))
    ]

    result = engine.run_suite(
        ParityConfig(
            artifact_dir=tmp_path / ".parity",
            jobs=2,
            native_threads=1,
            cases=cases,
        )
    )

    assert result.status is Status.FAILED
    assert [case.name for case in result.cases] == ["first", "second"]
    assert all(case.generated_examples > 0 for case in result.cases)
    artifacts = [case.failures[0].artifact for case in result.cases]
    assert all(artifact is not None for artifact in artifacts)
    assert [artifact.parent.name for artifact in artifacts if artifact is not None] == [
        "first",
        "second",
    ]
