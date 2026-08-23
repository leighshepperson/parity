"""Public Python API. The orchestration implementation lives in parity.engine."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hypothesis.strategies import SearchStrategy

from parity.config import load_config
from parity.invocation import Invocation
from parity.models import (
    AdapterName,
    CaseConfig,
    ComparisonPolicy,
    GenerationConfig,
    PandasInput,
    PerformanceConfig,
    SuiteResult,
)


def check(
    config: str | Path = "parity.toml",
    *,
    cases: set[str] | None = None,
    jobs: int | None = None,
    native_threads: int | None = None,
) -> SuiteResult:
    """Run cases from a validated parity.toml file."""

    if cases is not None and not cases:
        raise ValueError("cases must contain at least one case name when provided")

    from parity.engine import run_suite

    loaded = load_config(config)
    if jobs is not None:
        loaded.jobs = jobs
    if native_threads is not None:
        loaded.native_threads = native_threads
    return run_suite(loaded, selected_cases=cases)


def verify(
    reference: Callable[..., Any],
    candidate: Callable[..., Any],
    *,
    invocation: Invocation,
    strategy: SearchStrategy[Invocation] | None = None,
    comparison: ComparisonPolicy | None = None,
    generation: GenerationConfig | None = None,
    performance: PerformanceConfig | None = None,
    artifact_dir: str | Path = ".parity",
    reference_adapter: AdapterName = "auto",
    candidate_adapter: AdapterName = "auto",
    reference_pandas_input: PandasInput = "arrow",
    candidate_pandas_input: PandasInput = "arrow",
    reference_distributions: Sequence[str] = (),
    candidate_distributions: Sequence[str] = (),
) -> SuiteResult:
    """Verify two live callables without requiring a configuration file.

    Auto mode infers annotated dataframe implementations and otherwise uses the
    dependency-light Arrow adapter, so ordinary JSON calls need no dataframe
    dependency. Explicit adapters are recommended for unannotated dataframe
    functions or functions accepting different dataframe implementations.
    Pandas inputs use Arrow-backed extension dtypes by default; select ``native``
    materialization only when a callable depends on pandas' conventional
    NumPy/object dtype behavior.
    ``reference_distributions`` and ``candidate_distributions`` add explicitly
    named target-library versions to each side's runtime provenance.
    ``invocation`` represents the complete positional and keyword call. An
    optional Hypothesis strategy can generate and shrink additional invocations.
    """

    from parity.engine import run_live

    return run_live(
        reference,
        candidate,
        invocation=invocation,
        strategy=strategy,
        comparison=comparison or ComparisonPolicy(),
        generation=generation or GenerationConfig(),
        performance=performance or PerformanceConfig(),
        artifact_dir=Path(artifact_dir),
        reference_adapter=reference_adapter,
        candidate_adapter=candidate_adapter,
        reference_pandas_input=reference_pandas_input,
        candidate_pandas_input=candidate_pandas_input,
        reference_distributions=reference_distributions,
        candidate_distributions=candidate_distributions,
    )


__all__ = ["CaseConfig", "check", "verify"]
