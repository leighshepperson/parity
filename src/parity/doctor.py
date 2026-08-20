"""Environment diagnostics used by the CLI and support bundles."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from parity._version import __version__
from parity.provenance import distribution_satisfies_requirement

if TYPE_CHECKING:
    from parity.execution import Observation
    from parity.models import CallableSpec, CaseConfig, ParityConfig


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
    requirement: str | None = None
    satisfied: bool | None = None

    @property
    def healthy(self) -> bool:
        return self.status == "installed" and self.satisfied is not False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "version": self.version,
        }
        if self.requirement is not None:
            payload["requirement"] = self.requirement
            payload["satisfied"] = self.satisfied
        return payload


@dataclass(frozen=True, slots=True)
class WorkerRuntimeReport:
    """Data-safe readiness evidence collected inside one configured worker."""

    status: Literal["ready", "not_checked", "error", "crashed", "timed_out", "invalid_response"]
    error_code: str | None = None
    executor: Literal["parity-python", "portable-python", "command"] | None = None
    runtime_name: str | None = None
    runtime_version: str | None = None
    python_implementation: str | None = None
    python_version: str | None = None
    parity_version: str | None = None
    parity_satisfied: bool | None = None
    distributions: tuple[RecordedDistributionStatus, ...] = ()

    @property
    def healthy(self) -> bool:
        return (
            self.status == "ready"
            and self.parity_satisfied is not False
            and all(item.healthy for item in self.distributions)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "healthy": self.healthy,
            "error_code": self.error_code,
            "executor": self.executor,
            "runtime": (
                {"name": self.runtime_name, "version": self.runtime_version}
                if self.runtime_name is not None and self.runtime_version is not None
                else None
            ),
            "python": (
                {
                    "implementation": self.python_implementation,
                    "version": self.python_version,
                }
                if self.python_implementation is not None and self.python_version is not None
                else None
            ),
            "parity": self.parity_version,
            "parity_satisfied": self.parity_satisfied,
            "distributions": [item.to_dict() for item in self.distributions],
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
    """Configured transport and endpoint readiness without target invocation."""

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
    "packaging",
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


def _worker_report(spec: CallableSpec, observation: Observation) -> WorkerRuntimeReport:
    """Convert one data-safe preflight observation into a readiness report."""

    from parity.execution import ExecutionOutcome

    runtime = observation.runtime
    unavailable = tuple(
        RecordedDistributionStatus(
            name=name,
            status="unavailable",
            version=None,
            requirement=spec.required_distributions.get(name),
            satisfied=False if name in spec.required_distributions else None,
        )
        for name in spec.provenance_distributions
    )
    if runtime is None:
        if observation.outcome in {ExecutionOutcome.CRASHED, ExecutionOutcome.TIMED_OUT}:
            status: Literal["crashed", "timed_out"] = (
                "timed_out" if observation.outcome is ExecutionOutcome.TIMED_OUT else "crashed"
            )
            return WorkerRuntimeReport(
                status=status,
                error_code=(observation.exception.type if observation.exception else None),
                distributions=unavailable,
            )
        return WorkerRuntimeReport(
            status="invalid_response",
            error_code=(observation.exception.type if observation.exception else None),
            distributions=unavailable,
        )

    by_name = {distribution.name: distribution for distribution in runtime.distributions}
    distributions: list[RecordedDistributionStatus] = []
    for name in spec.provenance_distributions:
        distribution = by_name.get(name)
        requirement = spec.required_distributions.get(name)
        if distribution is None:
            distributions.append(
                RecordedDistributionStatus(
                    name=name,
                    status="unavailable",
                    version=None,
                    requirement=requirement,
                    satisfied=False if requirement is not None else None,
                )
            )
        else:
            distributions.append(
                RecordedDistributionStatus(
                    name=name,
                    status=distribution.status,
                    version=distribution.version,
                    requirement=requirement,
                    satisfied=(
                        distribution.status == "installed"
                        and distribution_satisfies_requirement(distribution.version, requirement)
                        if requirement is not None
                        else None
                    ),
                )
            )
    worker_status: Literal["ready", "error", "crashed", "timed_out", "invalid_response"]
    if observation.outcome is ExecutionOutcome.RETURNED:
        worker_status = "ready"
    elif observation.outcome is ExecutionOutcome.CRASHED:
        worker_status = "crashed"
    elif observation.outcome is ExecutionOutcome.TIMED_OUT:
        worker_status = "timed_out"
    else:
        worker_status = "error"
    return WorkerRuntimeReport(
        status=worker_status,
        error_code=(
            observation.exception.type
            if observation.outcome is not ExecutionOutcome.RETURNED
            and observation.exception is not None
            else None
        ),
        executor=runtime.executor,
        runtime_name=runtime.runtime_name,
        runtime_version=runtime.runtime_version,
        python_implementation=runtime.python_implementation,
        python_version=runtime.python_version,
        parity_version=runtime.parity_version,
        parity_satisfied=(
            runtime.parity_version == __version__ if runtime.executor == "parity-python" else None
        ),
        distributions=tuple(distributions),
    )


def _inspect_case(case: CaseConfig) -> CaseRuntimeReport:
    """Preflight both transports before either endpoint can be imported."""

    from parity.execution import ExecutionOutcome, IsolatedExecutionSession

    timeout_seconds = case.timeout_seconds
    with (
        IsolatedExecutionSession(case.reference, timeout_seconds=timeout_seconds) as reference,
        IsolatedExecutionSession(case.candidate, timeout_seconds=timeout_seconds) as candidate,
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="parity-doctor") as pool,
    ):
        reference_future = pool.submit(reference.preflight_transport)
        candidate_future = pool.submit(candidate.preflight_transport)
        reference_probe = reference_future.result()
        candidate_probe = candidate_future.result()

        reference_ready = reference_probe.outcome is ExecutionOutcome.RETURNED
        candidate_ready = candidate_probe.outcome is ExecutionOutcome.RETURNED
        if reference_ready and candidate_ready:
            reference_future = pool.submit(reference.preflight_endpoint)
            candidate_future = pool.submit(candidate.preflight_endpoint)
            reference_probe = reference_future.result()
            candidate_probe = candidate_future.result()
            return CaseRuntimeReport(
                name=case.name,
                reference=_worker_report(case.reference, reference_probe),
                candidate=_worker_report(case.candidate, candidate_probe),
            )

        reference_report = _worker_report(case.reference, reference_probe)
        candidate_report = _worker_report(case.candidate, candidate_probe)
        if reference_ready:
            reference_report = replace(
                reference_report,
                status="not_checked",
                error_code="TargetEndpointNotChecked",
            )
        if candidate_ready:
            candidate_report = replace(
                candidate_report,
                status="not_checked",
                error_code="TargetEndpointNotChecked",
            )
        return CaseRuntimeReport(
            name=case.name,
            reference=reference_report,
            candidate=candidate_report,
        )


def diagnose_config(config: ParityConfig, *, case_name: str | None = None) -> ConfigDoctorReport:
    """Preflight selected target runtimes and imports without invoking targets."""

    cases = config.cases
    if case_name is not None:
        cases = [case for case in cases if case.name == case_name]
        if not cases:
            raise ValueError(f"unknown case: {case_name}")
    return ConfigDoctorReport(cases=tuple(_inspect_case(case) for case in cases))
