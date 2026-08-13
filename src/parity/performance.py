"""Repeatable, policy-driven reference/candidate benchmarks."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence

import pyarrow as pa

from parity.execution import Observation, execute
from parity.models import (
    CallableSpec,
    JsonValue,
    PerformanceConfig,
    PerformanceResult,
    RunMetrics,
)


class BenchmarkError(RuntimeError):
    """Raised when an implementation cannot complete a benchmark invocation."""


def _require_success(label: str, observation: Observation) -> Observation:
    if observation.succeeded:
        return observation
    kind = observation.exception.type if observation.exception else observation.outcome.value
    raise BenchmarkError(f"{label} benchmark did not return successfully ({kind})")


def _aggregate(observations: list[Observation]) -> RunMetrics:
    durations = [observation.metrics.duration_seconds for observation in observations]
    memories = [
        observation.metrics.peak_rss_bytes
        for observation in observations
        if observation.metrics.peak_rss_bytes is not None
    ]
    return RunMetrics(
        duration_seconds=float(statistics.median(durations)),
        peak_rss_bytes=int(statistics.median(memories)) if memories else None,
        iterations=len(observations),
    )


def benchmark_observations(
    reference_runner: Callable[[], Observation],
    candidate_runner: Callable[[], Observation],
    config: PerformanceConfig,
) -> PerformanceResult:
    """Benchmark two zero-argument observation runners.

    Implementations are deliberately interleaved to reduce systematic effects
    from thermal throttling and unrelated host load.  The median is reported so
    one noisy CI iteration does not dominate the policy result.
    """

    for _ in range(config.warmups):
        _require_success("reference warmup", reference_runner())
        _require_success("candidate warmup", candidate_runner())

    reference_runs: list[Observation] = []
    candidate_runs: list[Observation] = []
    for index in range(config.repeats):
        # Reverse alternate pairs to avoid always giving one implementation the
        # first or second position in a pair.
        if index % 2:
            candidate_runs.append(_require_success("candidate", candidate_runner()))
            reference_runs.append(_require_success("reference", reference_runner()))
        else:
            reference_runs.append(_require_success("reference", reference_runner()))
            candidate_runs.append(_require_success("candidate", candidate_runner()))

    reference = _aggregate(reference_runs)
    candidate = _aggregate(candidate_runs)
    speed_ratio = (
        candidate.duration_seconds / reference.duration_seconds
        if reference.duration_seconds > 0
        else None
    )
    memory_ratio = (
        candidate.peak_rss_bytes / reference.peak_rss_bytes
        if candidate.peak_rss_bytes is not None
        and reference.peak_rss_bytes is not None
        and reference.peak_rss_bytes > 0
        else None
    )
    reasons: list[str] = []
    reference_milliseconds = reference.duration_seconds * 1_000
    if (
        config.max_slowdown is not None
        and speed_ratio is not None
        and math.isfinite(speed_ratio)
        and reference_milliseconds >= config.min_reference_ms
        and speed_ratio > config.max_slowdown
    ):
        reasons.append(
            f"candidate median runtime is {speed_ratio:.3f}x reference "
            f"(limit {config.max_slowdown:.3f}x)"
        )
    if (
        config.max_memory_ratio is not None
        and memory_ratio is not None
        and math.isfinite(memory_ratio)
        and memory_ratio > config.max_memory_ratio
    ):
        reasons.append(
            f"candidate peak RSS is {memory_ratio:.3f}x reference "
            f"(limit {config.max_memory_ratio:.3f}x)"
        )
    return PerformanceResult(
        reference=reference,
        candidate=candidate,
        speed_ratio=speed_ratio,
        memory_ratio=memory_ratio,
        regression=bool(reasons),
        reasons=reasons,
    )


def benchmark_pair(
    reference: CallableSpec,
    candidate: CallableSpec,
    input_table: pa.Table,
    config: PerformanceConfig,
    *,
    static_args: Sequence[JsonValue] = (),
    static_kwargs: Mapping[str, JsonValue] | None = None,
    timeout_seconds: float = 30.0,
    isolated: bool | None = None,
) -> PerformanceResult:
    """Benchmark a configured implementation pair against one canonical input."""

    if not config.enabled:
        raise BenchmarkError("performance benchmarking is disabled")

    def run(spec: CallableSpec) -> Observation:
        return execute(
            spec,
            input_table,
            static_args=static_args,
            static_kwargs=static_kwargs,
            isolated=isolated,
            timeout_seconds=timeout_seconds,
        )

    return benchmark_observations(lambda: run(reference), lambda: run(candidate), config)


__all__ = ["BenchmarkError", "benchmark_observations", "benchmark_pair"]
