from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from parity.engine import run_suite
from parity.models import (
    CallableSpec,
    CaseConfig,
    GenerationConfig,
    ParityConfig,
    PerformanceConfig,
    Status,
)


@pytest.mark.integration
def test_isolated_cpu_heavy_campaign_enforces_statistical_slowdown_gate(
    tmp_path: Path,
) -> None:
    """Exercise the real process profiler, not fabricated timing observations."""

    (tmp_path / "cpu_heavy_targets.py").write_text(
        "def _burn(rounds):\n"
        "    total = 0\n"
        "    for index in range(rounds):\n"
        "        total = (total + (index ^ (index >> 3))) & 0xFFFFFFFF\n"
        "    return total\n"
        "def reference(frame):\n"
        "    assert _burn(50_000) >= 0\n"
        "    return frame\n"
        "def candidate(frame):\n"
        "    assert _burn(1_000_000) >= 0\n"
        "    return frame\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"value": [1, 2, 3]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    case = CaseConfig(
        name="cpu-slowdown",
        reference=CallableSpec(
            target="cpu_heavy_targets:reference",
            adapter="arrow",
            workdir=tmp_path,
            native_threads=1,
        ),
        candidate=CallableSpec(
            target="cpu_heavy_targets:candidate",
            adapter="arrow",
            workdir=tmp_path,
            native_threads=1,
        ),
        fixture=fixture,
        generation=GenerationConfig(
            max_examples=1,
            stability_repeats=1,
            search=False,
            adversarial_examples=False,
        ),
        performance=PerformanceConfig(
            enabled=True,
            warmups=1,
            repeats=5,
            max_slowdown=3,
            max_memory_ratio=None,
            min_reference_ms=0,
            fail_on_regression=True,
            bootstrap_samples=500,
        ),
    )

    result = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    observed = result.cases[0]
    assert result.status is observed.status is Status.FAILED
    assert observed.performance is not None
    assert observed.performance.regression
    assert observed.performance.speed_ratio is not None
    assert observed.performance.speed_ratio > 3
    assert observed.performance.speed_ratio_ci is not None
    assert observed.performance.speed_ratio_ci[0] > 3
    assert observed.performance.confidence_level == pytest.approx(0.95)
    assert observed.performance.reference.iterations == 5
    assert observed.performance.candidate.iterations == 5
    assert observed.failures[-1].source == "performance"
    assert observed.failures[-1].status is Status.FAILED
    assert "paired median runtime" in observed.failures[-1].mismatches[0].message
