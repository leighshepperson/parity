from __future__ import annotations

from collections.abc import Callable

import pytest

from parity.execution import ExceptionInfo, ExecutionOutcome, Observation
from parity.models import PerformanceConfig, RunMetrics
from parity.performance import BenchmarkError, benchmark_observations


def _runner(duration: float, memory: int) -> Callable[[], Observation]:
    return lambda: Observation(
        outcome=ExecutionOutcome.RETURNED,
        metrics=RunMetrics(duration_seconds=duration, peak_rss_bytes=memory),
        has_value=True,
        value=True,
    )


def test_benchmark_aggregates_and_applies_thresholds() -> None:
    result = benchmark_observations(
        _runner(0.01, 100),
        _runner(0.02, 200),
        PerformanceConfig(
            warmups=1,
            repeats=3,
            max_slowdown=1.5,
            max_memory_ratio=1.5,
            min_reference_ms=0,
        ),
    )
    assert result.reference.iterations == 3
    assert result.candidate.iterations == 3
    assert result.speed_ratio == pytest.approx(2)
    assert result.memory_ratio == pytest.approx(2)
    assert result.regression
    assert len(result.reasons) == 2


def test_short_reference_does_not_trigger_runtime_threshold() -> None:
    result = benchmark_observations(
        _runner(0.0001, 100),
        _runner(0.001, 100),
        PerformanceConfig(
            warmups=0,
            repeats=1,
            max_slowdown=1.1,
            max_memory_ratio=None,
            min_reference_ms=1,
        ),
    )
    assert result.speed_ratio == pytest.approx(10)
    assert not result.regression


def test_benchmark_stops_on_failed_observation_without_message_data() -> None:
    def failed() -> Observation:
        return Observation(
            outcome=ExecutionOutcome.RAISED,
            metrics=RunMetrics(duration_seconds=0),
            exception=ExceptionInfo("builtins", "ValueError", "sensitive row"),
        )

    with pytest.raises(BenchmarkError, match="ValueError") as captured:
        benchmark_observations(
            failed,
            _runner(0.1, 100),
            PerformanceConfig(warmups=0, repeats=1),
        )
    assert "sensitive row" not in str(captured.value)
