from __future__ import annotations

import json
import time
from collections.abc import Callable

import pytest

from parity.execution import ExceptionInfo, ExecutionOutcome, Observation
from parity.models import CaseResult, PerformanceConfig, RunMetrics, Status, SuiteResult
from parity.performance import BenchmarkError, benchmark_observations
from parity.reporting import render_json


def _runner(duration: float, memory: int | None) -> Callable[[], Observation]:
    return lambda: Observation(
        outcome=ExecutionOutcome.RETURNED,
        metrics=RunMetrics(duration_seconds=duration, peak_rss_bytes=memory),
        has_value=True,
        value=True,
    )


def _sequence_runner(durations: list[float], memory: int = 100) -> Callable[[], Observation]:
    remaining = iter(durations)
    return lambda: Observation(
        outcome=ExecutionOutcome.RETURNED,
        metrics=RunMetrics(duration_seconds=next(remaining), peak_rss_bytes=memory),
        has_value=True,
        value=True,
    )


def _unchecked_runner(duration: float, memory: int | None) -> Callable[[], Observation]:
    """Simulate an untrusted transport bypassing RunMetrics validation."""

    return lambda: Observation(
        outcome=ExecutionOutcome.RETURNED,
        metrics=RunMetrics.model_construct(
            duration_seconds=duration,
            peak_rss_bytes=memory,
        ),
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
    assert result.speed_ratio_ci == pytest.approx((2, 2))
    assert result.memory_ratio == pytest.approx(2)
    assert result.memory_ratio_ci == pytest.approx((2, 2))
    assert result.confidence_level == pytest.approx(0.95)
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


def test_noisy_point_estimate_does_not_fail_when_interval_crosses_limit() -> None:
    result = benchmark_observations(
        _sequence_runner([0.01] * 5),
        _sequence_runner([0.01, 0.011, 0.02, 0.021, 0.022]),
        PerformanceConfig(
            warmups=0,
            repeats=5,
            max_slowdown=1.5,
            max_memory_ratio=None,
            min_reference_ms=0,
        ),
    )

    assert result.speed_ratio == pytest.approx(2)
    assert result.speed_ratio_ci is not None
    assert result.speed_ratio_ci[0] <= 1.5
    assert not result.regression


def test_enforced_gate_requires_enough_observations() -> None:
    with pytest.raises(ValueError, match="at least 5 repeats"):
        PerformanceConfig(repeats=4, fail_on_regression=True)


@pytest.mark.parametrize("invalid_duration", [-0.01, 0.0, float("inf")])
def test_enforced_runtime_gate_rejects_invalid_reference_timing(
    invalid_duration: float,
) -> None:
    with pytest.raises(BenchmarkError, match=r"reference timing evidence.*0/5 runs"):
        benchmark_observations(
            _unchecked_runner(invalid_duration, 100),
            _unchecked_runner(invalid_duration, 100),
            PerformanceConfig(
                warmups=0,
                repeats=5,
                max_slowdown=1.5,
                max_memory_ratio=None,
                min_reference_ms=1,
                fail_on_regression=True,
            ),
        )


def test_enforced_runtime_gate_rejects_incomplete_paired_timing() -> None:
    with pytest.raises(BenchmarkError, match=r"timing evidence.*4/5 pairs"):
        benchmark_observations(
            _sequence_runner([0.01] * 5),
            _sequence_runner([0.02, 0.02, 0.0, 0.02, 0.02]),
            PerformanceConfig(
                warmups=0,
                repeats=5,
                max_slowdown=1.5,
                max_memory_ratio=None,
                min_reference_ms=1,
                fail_on_regression=True,
            ),
        )


def test_enforced_runtime_gate_rejects_nonfinite_ratio_evidence() -> None:
    with pytest.raises(BenchmarkError, match="finite positive runtime ratio"):
        benchmark_observations(
            _runner(1e-300, 100),
            _runner(1e300, 100),
            PerformanceConfig(
                warmups=0,
                repeats=5,
                max_slowdown=1.5,
                max_memory_ratio=None,
                min_reference_ms=0,
                fail_on_regression=True,
            ),
        )


def test_unusable_advisory_runtime_ratio_remains_json_reportable() -> None:
    performance = benchmark_observations(
        _runner(1e-300, 100),
        _runner(1e300, 100),
        PerformanceConfig(
            warmups=0,
            repeats=1,
            max_slowdown=1.5,
            max_memory_ratio=None,
            min_reference_ms=0,
            fail_on_regression=False,
        ),
    )
    suite = SuiteResult(
        status=Status.PASSED,
        cases=[
            CaseResult(
                name="advisory-performance",
                status=Status.PASSED,
                performance=performance,
            )
        ],
    )

    assert performance.speed_ratio is None
    assert performance.speed_ratio_ci is None
    payload = json.loads(render_json(suite))
    assert payload["cases"][0]["performance"]["speed_ratio"] is None
    assert payload["cases"][0]["performance"]["speed_ratio_ci"] is None


def test_enforced_runtime_gate_preserves_valid_minimum_reference_skip() -> None:
    result = benchmark_observations(
        _runner(0.0001, 100),
        _runner(0.001, 100),
        PerformanceConfig(
            warmups=0,
            repeats=5,
            max_slowdown=1.1,
            max_memory_ratio=None,
            min_reference_ms=1,
            fail_on_regression=True,
        ),
    )

    assert result.speed_ratio == pytest.approx(10)
    assert result.speed_ratio_ci == pytest.approx((10, 10))
    assert not result.regression


def test_enforced_memory_gate_errors_without_complete_peak_rss_evidence() -> None:
    with pytest.raises(BenchmarkError, match=r"peak RSS evidence.*0/5 pairs"):
        benchmark_observations(
            _runner(0.01, None),
            _runner(0.01, None),
            PerformanceConfig(
                warmups=0,
                repeats=5,
                max_slowdown=None,
                max_memory_ratio=1.5,
                fail_on_regression=True,
            ),
        )


def test_observational_memory_check_can_report_unavailable_evidence() -> None:
    result = benchmark_observations(
        _runner(0.01, None),
        _runner(0.01, None),
        PerformanceConfig(
            warmups=0,
            repeats=1,
            max_slowdown=None,
            max_memory_ratio=1.5,
            fail_on_regression=False,
        ),
    )

    assert result.memory_ratio is None
    assert result.memory_ratio_ci is None


def test_cpu_heavy_candidate_is_detected_without_sleep_timing() -> None:
    def cpu_runner(multiplier: int) -> Callable[[], Observation]:
        def run() -> Observation:
            started = time.perf_counter()
            checksum = 0
            for _ in range(multiplier):
                for value in range(80_000):
                    checksum = (checksum + value * value) % 1_000_003
            return Observation(
                outcome=ExecutionOutcome.RETURNED,
                metrics=RunMetrics(duration_seconds=time.perf_counter() - started),
                has_value=True,
                value=checksum,
            )

        return run

    result = benchmark_observations(
        cpu_runner(1),
        cpu_runner(5),
        PerformanceConfig(
            warmups=1,
            repeats=7,
            max_slowdown=2,
            max_memory_ratio=None,
            min_reference_ms=0,
            bootstrap_samples=1_000,
        ),
    )

    assert result.speed_ratio_ci is not None
    assert result.speed_ratio_ci[0] > 2
    assert result.regression


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
