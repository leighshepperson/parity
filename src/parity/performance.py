"""Repeatable, policy-driven reference/candidate benchmarks."""

from __future__ import annotations

import math
import random
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


def _finite_positive(value: float) -> bool:
    """Return whether a metric can participate in a multiplicative ratio."""

    return value > 0 and math.isfinite(value)


def _exp_ratio(log_ratio: float) -> float | None:
    """Exponentiate a log ratio, returning no evidence on numeric overflow."""

    try:
        ratio = math.exp(log_ratio)
    except OverflowError:
        return None
    return ratio if _finite_positive(ratio) else None


def _quantile(values: list[float], probability: float) -> float:
    """Return a linearly interpolated quantile for an already bounded probability."""

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _paired_ratio_interval(
    reference: list[float],
    candidate: list[float],
    *,
    confidence_level: float,
    bootstrap_samples: int,
    seed: int,
) -> tuple[float | None, tuple[float, float] | None]:
    """Return a paired median ratio and deterministic bootstrap interval.

    Pairing keeps each candidate observation next to the adjacent reference
    observation taken under approximately the same host load.  Bootstrapping
    the median of log-ratios avoids a single noisy timing turning into a gate.
    The fixed seed makes the reported decision reproducible from the evidence.
    """

    log_ratios = [
        math.log(candidate_value) - math.log(reference_value)
        for reference_value, candidate_value in zip(reference, candidate, strict=True)
        if _finite_positive(reference_value) and _finite_positive(candidate_value)
    ]
    if not log_ratios:
        return None, None
    point = _exp_ratio(statistics.median(log_ratios))
    if point is None:
        return None, None
    if len(log_ratios) == 1:
        return point, (point, point)

    generator = random.Random(seed)
    count = len(log_ratios)
    bootstrapped: list[float] = []
    for _ in range(bootstrap_samples):
        ratio = _exp_ratio(statistics.median(generator.choices(log_ratios, k=count)))
        if ratio is None:
            return None, None
        bootstrapped.append(ratio)
    tail = (1 - confidence_level) / 2
    interval = (_quantile(bootstrapped, tail), _quantile(bootstrapped, 1 - tail))
    if not all(_finite_positive(bound) for bound in interval):
        return None, None
    return point, interval


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

    reference_durations = [item.metrics.duration_seconds for item in reference_runs]
    candidate_durations = [item.metrics.duration_seconds for item in candidate_runs]
    enforced_runtime_gate = config.fail_on_regression and config.max_slowdown is not None
    runtime_gate_applies = False
    if enforced_runtime_gate:
        usable_reference_runs = sum(_finite_positive(value) for value in reference_durations)
        if usable_reference_runs != config.repeats:
            raise BenchmarkError(
                "enforced runtime gate requires finite positive reference timing evidence "
                f"for every run ({usable_reference_runs}/{config.repeats} runs observed)"
            )
        reference_milliseconds = float(statistics.median(reference_durations)) * 1_000
        runtime_gate_applies = reference_milliseconds >= config.min_reference_ms
        if runtime_gate_applies:
            usable_pairs = sum(
                _finite_positive(reference_value) and _finite_positive(candidate_value)
                for reference_value, candidate_value in zip(
                    reference_durations, candidate_durations, strict=True
                )
            )
            if usable_pairs != config.repeats:
                raise BenchmarkError(
                    "enforced runtime gate requires finite positive timing evidence for every "
                    f"paired run ({usable_pairs}/{config.repeats} pairs observed)"
                )
    usable_duration_observations = sum(
        value >= 0 and math.isfinite(value)
        for value in [*reference_durations, *candidate_durations]
    )
    expected_duration_observations = config.repeats * 2
    if usable_duration_observations != expected_duration_observations:
        raise BenchmarkError(
            "benchmark requires finite non-negative timing metrics for every observation "
            f"({usable_duration_observations}/{expected_duration_observations} observed)"
        )
    reference = _aggregate(reference_runs)
    candidate = _aggregate(candidate_runs)
    speed_ratio, speed_ratio_ci = _paired_ratio_interval(
        reference_durations,
        candidate_durations,
        confidence_level=config.confidence_level,
        bootstrap_samples=config.bootstrap_samples,
        seed=0x50415249,
    )
    if runtime_gate_applies and (
        speed_ratio is None
        or speed_ratio_ci is None
        or not _finite_positive(speed_ratio)
        or not all(_finite_positive(bound) for bound in speed_ratio_ci)
    ):
        raise BenchmarkError(
            "enforced runtime gate requires a finite positive runtime ratio and confidence interval"
        )
    paired_memory = [
        (reference_item.metrics.peak_rss_bytes, candidate_item.metrics.peak_rss_bytes)
        for reference_item, candidate_item in zip(reference_runs, candidate_runs, strict=True)
        if reference_item.metrics.peak_rss_bytes is not None
        and candidate_item.metrics.peak_rss_bytes is not None
    ]
    if (
        config.fail_on_regression
        and config.max_memory_ratio is not None
        and len(paired_memory) != config.repeats
    ):
        raise BenchmarkError(
            "enforced memory gate requires peak RSS evidence for every paired run "
            f"({len(paired_memory)}/{config.repeats} pairs observed)"
        )
    memory_ratio, memory_ratio_ci = _paired_ratio_interval(
        [float(reference_value) for reference_value, _ in paired_memory],
        [float(candidate_value) for _, candidate_value in paired_memory],
        confidence_level=config.confidence_level,
        bootstrap_samples=config.bootstrap_samples,
        seed=0x4D454D4F,
    )
    if (
        config.fail_on_regression
        and config.max_memory_ratio is not None
        and (memory_ratio is None or memory_ratio_ci is None)
    ):
        raise BenchmarkError("enforced memory gate requires usable non-zero peak RSS evidence")
    reasons: list[str] = []
    reference_milliseconds = reference.duration_seconds * 1_000
    if (
        config.max_slowdown is not None
        and speed_ratio is not None
        and speed_ratio_ci is not None
        and math.isfinite(speed_ratio)
        and reference_milliseconds >= config.min_reference_ms
        and speed_ratio_ci[0] > config.max_slowdown
    ):
        reasons.append(
            f"candidate paired median runtime is {speed_ratio:.3f}x reference "
            f"({config.confidence_level:.0%} CI {speed_ratio_ci[0]:.3f}-"
            f"{speed_ratio_ci[1]:.3f}x; limit {config.max_slowdown:.3f}x)"
        )
    if (
        config.max_memory_ratio is not None
        and memory_ratio is not None
        and memory_ratio_ci is not None
        and math.isfinite(memory_ratio)
        and memory_ratio_ci[0] > config.max_memory_ratio
    ):
        reasons.append(
            f"candidate paired median peak RSS is {memory_ratio:.3f}x reference "
            f"({config.confidence_level:.0%} CI {memory_ratio_ci[0]:.3f}-"
            f"{memory_ratio_ci[1]:.3f}x; limit {config.max_memory_ratio:.3f}x)"
        )
    return PerformanceResult(
        reference=reference,
        candidate=candidate,
        speed_ratio=speed_ratio,
        speed_ratio_ci=speed_ratio_ci,
        memory_ratio=memory_ratio,
        memory_ratio_ci=memory_ratio_ci,
        confidence_level=config.confidence_level,
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
