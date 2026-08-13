"""Public Python API. The orchestration implementation lives in parity.engine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from parity.config import load_config
from parity.models import (
    AdapterName,
    CaseConfig,
    ComparisonPolicy,
    FrameSchema,
    GenerationConfig,
    PerformanceConfig,
    SuiteResult,
)


def check(config: str | Path = "parity.toml", *, cases: set[str] | None = None) -> SuiteResult:
    """Run cases from a validated parity.toml file."""

    from parity.engine import run_suite

    return run_suite(load_config(config), selected_cases=cases)


def verify(
    reference: Callable[..., Any],
    candidate: Callable[..., Any],
    *,
    fixture: Any | None = None,
    schema: FrameSchema | None = None,
    comparison: ComparisonPolicy | None = None,
    generation: GenerationConfig | None = None,
    performance: PerformanceConfig | None = None,
    artifact_dir: str | Path = ".parity",
    reference_adapter: AdapterName = "auto",
    candidate_adapter: AdapterName = "auto",
) -> SuiteResult:
    """Verify two live callables without requiring a configuration file.

    Explicit adapters are recommended when the functions are unannotated or
    accept different dataframe implementations.
    """

    from parity.engine import run_live

    return run_live(
        reference,
        candidate,
        fixture=fixture,
        schema=schema,
        comparison=comparison or ComparisonPolicy(),
        generation=generation or GenerationConfig(),
        performance=performance or PerformanceConfig(),
        artifact_dir=Path(artifact_dir),
        reference_adapter=reference_adapter,
        candidate_adapter=candidate_adapter,
    )


__all__ = ["CaseConfig", "check", "verify"]
