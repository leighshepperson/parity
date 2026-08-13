"""Environment diagnostics used by the CLI and support bundles."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from parity.models import CallableSpec, ParityConfig


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    name: str
    installed: bool
    version: str | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    python: str
    executable: str
    platform: str
    working_directory: str
    dependencies: tuple[DependencyStatus, ...]

    @property
    def healthy(self) -> bool:
        return all(dependency.installed for dependency in self.dependencies)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RecordedDistributionStatus:
    """One explicitly requested, path-free distribution observation."""

    name: str
    status: Literal["installed", "missing", "unavailable"]
    version: str | None

    @property
    def healthy(self) -> bool:
        return self.status == "installed"


@dataclass(frozen=True, slots=True)
class WorkerRuntimeReport:
    """Data-safe readiness evidence collected inside one configured worker."""

    status: Literal["ready", "crashed", "timed_out", "invalid_response"]
    python_implementation: str | None = None
    python_version: str | None = None
    parity_version: str | None = None
    distributions: tuple[RecordedDistributionStatus, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.status == "ready" and all(item.healthy for item in self.distributions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "healthy": self.healthy,
            "python": (
                {
                    "implementation": self.python_implementation,
                    "version": self.python_version,
                }
                if self.python_implementation is not None and self.python_version is not None
                else None
            ),
            "parity": self.parity_version,
            "distributions": [asdict(item) for item in self.distributions],
        }


@dataclass(frozen=True, slots=True)
class CaseRuntimeReport:
    """Side-by-side worker readiness for one configured case."""

    name: str
    reference: WorkerRuntimeReport
    candidate: WorkerRuntimeReport

    @property
    def healthy(self) -> bool:
        return self.reference.healthy and self.candidate.healthy

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "reference": self.reference.to_dict(),
            "candidate": self.candidate.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ConfigDoctorReport:
    """Configured worker readiness without callable import or invocation."""

    cases: tuple[CaseRuntimeReport, ...]

    @property
    def healthy(self) -> bool:
        return all(case.healthy for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "cases": [case.to_dict() for case in self.cases],
        }


REQUIRED_DEPENDENCIES = (
    "hypothesis",
    "numpy",
    "pandas",
    "polars",
    "psutil",
    "pyarrow",
    "pydantic",
    "rich",
    "typer",
)


def dependency_status(name: str) -> DependencyStatus:
    try:
        importlib.import_module(name)
        version = importlib.metadata.version(name)
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        return DependencyStatus(name=name, installed=False, version=None, detail=str(exc))
    return DependencyStatus(name=name, installed=True, version=version)


def diagnose() -> DoctorReport:
    """Return deterministic diagnostics without exposing environment variables."""

    return DoctorReport(
        python=platform.python_version(),
        executable=sys.executable,
        platform=platform.platform(),
        working_directory=str(Path.cwd()),
        dependencies=tuple(dependency_status(name) for name in REQUIRED_DEPENDENCIES),
    )


def _inspect_worker(spec: CallableSpec, *, timeout_seconds: float) -> WorkerRuntimeReport:
    """Inspect one worker using only the provenance protocol operation."""

    from parity.execution import ExecutionOutcome, IsolatedExecutionSession

    with IsolatedExecutionSession(spec, timeout_seconds=timeout_seconds) as session:
        observation = session.inspect_runtime()
    runtime = observation.runtime
    unavailable = tuple(
        RecordedDistributionStatus(name=name, status="unavailable", version=None)
        for name in spec.record_distributions
    )
    if observation.outcome is not ExecutionOutcome.RETURNED:
        status: Literal["crashed", "timed_out"] = (
            "timed_out" if observation.outcome is ExecutionOutcome.TIMED_OUT else "crashed"
        )
        return WorkerRuntimeReport(status=status, distributions=unavailable)
    if runtime is None:
        return WorkerRuntimeReport(status="invalid_response", distributions=unavailable)

    by_name = {distribution.name: distribution for distribution in runtime.distributions}
    distributions: list[RecordedDistributionStatus] = []
    for name in spec.record_distributions:
        distribution = by_name.get(name)
        if distribution is None:
            distributions.append(
                RecordedDistributionStatus(name=name, status="unavailable", version=None)
            )
        else:
            distributions.append(
                RecordedDistributionStatus(
                    name=name,
                    status=distribution.status,
                    version=distribution.version,
                )
            )
    return WorkerRuntimeReport(
        status="ready",
        python_implementation=runtime.python_implementation,
        python_version=runtime.python_version,
        parity_version=runtime.parity_version,
        distributions=tuple(distributions),
    )


def diagnose_config(config: ParityConfig, *, case_name: str | None = None) -> ConfigDoctorReport:
    """Inspect selected configured workers without importing their targets."""

    cases = config.cases
    if case_name is not None:
        cases = [case for case in cases if case.name == case_name]
        if not cases:
            raise ValueError(f"unknown case: {case_name}")
    return ConfigDoctorReport(
        cases=tuple(
            CaseRuntimeReport(
                name=case.name,
                reference=_inspect_worker(case.reference, timeout_seconds=case.timeout_seconds),
                candidate=_inspect_worker(case.candidate, timeout_seconds=case.timeout_seconds),
            )
            for case in cases
        )
    )
