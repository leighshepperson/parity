"""Campaign orchestration for semantic and performance verification.

The engine is intentionally small enough to audit.  It coordinates the public
generation, execution, comparison, diagnosis, artifact, and reporting layers;
none of those layers decides the outcome on its behalf.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pyarrow as pa

from parity._version import __version__
from parity.adapters import load_arrow_fixture, to_arrow
from parity.artifacts import ArtifactStore
from parity.comparison import compare_observations
from parity.diagnostics import diagnose
from parity.execution import (
    ExecutionOutcome,
    IsolatedExecutionSession,
    Observation,
    execute_callable_current,
)
from parity.generation import GeneratedCase, adversarial_cases
from parity.models import (
    AdapterName,
    CallableSpec,
    CaseConfig,
    CaseProvenance,
    CaseResult,
    ComparisonPolicy,
    ExampleResult,
    FrameSchema,
    GenerationConfig,
    Mismatch,
    MismatchKind,
    PandasInput,
    ParityConfig,
    PerformanceConfig,
    PerformanceResult,
    Status,
    SuiteProvenance,
    SuiteResult,
)
from parity.performance import BenchmarkError, benchmark_observations
from parity.provenance import (
    collect_runtime_provenance,
    diff_runtime,
    effective_config_sha256,
    normalize_distribution_names,
)
from parity.schema import infer_schema
from parity.shrinking import find_counterexample


class ReplayError(ValueError):
    """Raised when an artifact cannot be safely or exactly replayed."""


Runner = Callable[[pa.Table], Observation]
PairRunner = Callable[[pa.Table], tuple[Observation, Observation]]


class _StopGeneratedCampaign(BaseException):
    """Control-flow signal preventing Hypothesis from shrinking operational errors."""


def _infrastructure_error(observation: Observation) -> bool:
    if observation.outcome in {ExecutionOutcome.CRASHED, ExecutionOutcome.TIMED_OUT}:
        return True
    exception = observation.exception
    return bool(
        exception
        and (
            exception.module.startswith("parity.")
            or exception.type
            in {"ExecutionError", "WorkerError", "WorkerProtocolError", "TimeoutError"}
        )
    )


def _infrastructure_mismatch(label: str, observation: Observation) -> Mismatch:
    exception = observation.exception
    kind = exception.type if exception else observation.outcome.value
    return Mismatch(
        kind=MismatchKind.EXCEPTION,
        message=f"{label} could not be executed ({kind})",
        path=f"${label}",
    )


def _observe_pair(
    table: pa.Table,
    reference_runner: Runner,
    candidate_runner: Runner,
    policy: ComparisonPolicy,
    pair_runner: PairRunner | None = None,
) -> tuple[Observation, Observation, list[Mismatch], Status]:
    if pair_runner is None:
        reference = reference_runner(table)
        candidate = candidate_runner(table)
    else:
        reference, candidate = pair_runner(table)
    infrastructure = []
    if _infrastructure_error(reference):
        infrastructure.append(_infrastructure_mismatch("reference", reference))
    if _infrastructure_error(candidate):
        infrastructure.append(_infrastructure_mismatch("candidate", candidate))
    if infrastructure:
        return reference, candidate, infrastructure, Status.ERROR
    mismatches = compare_observations(reference, candidate, policy)
    return (
        reference,
        candidate,
        mismatches,
        Status.FAILED if mismatches else Status.PASSED,
    )


def _example_result(
    source: str,
    reference: Observation,
    candidate: Observation,
    mismatches: list[Mismatch],
    status: Status,
) -> ExampleResult:
    return ExampleResult(
        source=source,
        status=status,
        mismatches=mismatches,
        reference_metrics=reference.metrics,
        candidate_metrics=candidate.metrics,
    )


def _store_failure(
    store: ArtifactStore,
    case: str | CaseConfig,
    table: pa.Table,
    result: ExampleResult,
    *,
    source: str,
    seed: int | None,
    reference: CallableSpec | None = None,
    candidate: CallableSpec | None = None,
    reference_observation: Observation,
    candidate_observation: Observation,
    config_sha256: str | None = None,
) -> None:
    result.artifact = store.write_failure(
        case,
        table,
        result,
        reference=reference,
        candidate=candidate,
        source=source,
        seed=seed,
        runtime_provenance=CaseProvenance(
            reference=reference_observation.runtime,
            candidate=candidate_observation.runtime,
        ),
        config_sha256=config_sha256,
    )


def _status_for(failures: list[ExampleResult], performance: PerformanceResult | None) -> Status:
    if any(failure.status is Status.ERROR for failure in failures):
        return Status.ERROR
    if failures or (performance is not None and performance.regression):
        return Status.FAILED
    return Status.PASSED


def _performance_failure(result: PerformanceResult) -> ExampleResult:
    return ExampleResult(
        source="performance",
        status=Status.FAILED,
        mismatches=[
            Mismatch(
                kind=MismatchKind.PERFORMANCE,
                message=reason,
                path="$performance",
            )
            for reason in result.reasons
        ],
    )


def _campaign(
    *,
    name: str,
    schema: FrameSchema,
    fixture: pa.Table | None,
    comparison: ComparisonPolicy,
    generation: GenerationConfig,
    performance_config: PerformanceConfig,
    artifact_store: ArtifactStore,
    reference_runner: Runner,
    candidate_runner: Runner,
    pair_runner: PairRunner | None = None,
    artifact_case: str | CaseConfig,
    reference_spec: CallableSpec | None = None,
    candidate_spec: CallableSpec | None = None,
    benchmark: Callable[[pa.Table], PerformanceResult] | None = None,
    exact_only: bool = False,
    expected_provenance: CaseProvenance | None = None,
    observed_provenance: CaseProvenance | None = None,
    config_sha256: str | None = None,
) -> CaseResult:
    started = time.perf_counter()
    failures: list[ExampleResult] = []
    deterministic_count = 0
    generated_count = 0
    representative = fixture
    reference_runtime = observed_provenance.reference if observed_provenance else None
    candidate_runtime = observed_provenance.candidate if observed_provenance else None

    def observe(table: pa.Table) -> tuple[Observation, Observation, list[Mismatch], Status]:
        nonlocal reference_runtime, candidate_runtime
        observed = _observe_pair(
            table,
            reference_runner,
            candidate_runner,
            comparison,
            pair_runner,
        )
        reference, candidate = observed[:2]
        reference_runtime = reference_runtime or reference.runtime
        candidate_runtime = candidate_runtime or candidate.runtime
        return observed

    deterministic: list[GeneratedCase]
    if exact_only:
        if fixture is None:
            raise ReplayError("the replay artifact has no input fixture")
        deterministic = [GeneratedCase("replay", fixture)]
    elif generation.adversarial_examples:
        deterministic = adversarial_cases(schema, fixture=fixture)
    elif fixture is not None:
        deterministic = [GeneratedCase("fixture", fixture)]
    else:
        deterministic = []

    for generated in deterministic:
        deterministic_count += 1
        if representative is None or (
            generated.table.num_rows > representative.num_rows and generated.table.num_rows > 0
        ):
            representative = generated.table
        reference, candidate, mismatches, status = observe(generated.table)
        if status is not Status.PASSED:
            failure = _example_result(generated.source, reference, candidate, mismatches, status)
            _store_failure(
                artifact_store,
                artifact_case,
                generated.table,
                failure,
                source=generated.source,
                seed=generation.seed,
                reference=reference_spec,
                candidate=candidate_spec,
                reference_observation=reference,
                candidate_observation=candidate,
                config_sha256=config_sha256,
            )
            failures.append(failure)
        if status is Status.ERROR:
            break

    # A deterministic failure is already a concrete, replayable witness.  A
    # second property search cannot change the campaign outcome and used to
    # launch hundreds of disposable workers for known-broken migrations.
    if not exact_only and not failures:
        latest: tuple[Observation, Observation, list[Mismatch], Status] | None = None
        latest_table: pa.Table | None = None

        def differs(table: pa.Table) -> bool:
            nonlocal generated_count, latest, latest_table
            generated_count += 1
            latest_table = table
            latest = observe(table)
            if latest[3] is Status.ERROR:
                # Operational failures are not a semantic property of the input.
                # Stop on the first one rather than repeatedly executing a
                # crashed or timed-out worker during Hypothesis shrinking.
                raise _StopGeneratedCampaign
            return latest[3] is not Status.PASSED

        try:
            counterexample = find_counterexample(schema, differs, generation)
        except _StopGeneratedCampaign:
            if latest is None or latest_table is None:  # pragma: no cover - defensive
                raise
            reference, candidate, mismatches, status = latest
            failure = _example_result("generated:error", reference, candidate, mismatches, status)
            _store_failure(
                artifact_store,
                artifact_case,
                latest_table,
                failure,
                source="generated:error",
                seed=generation.seed,
                reference=reference_spec,
                candidate=candidate_spec,
                reference_observation=reference,
                candidate_observation=candidate,
                config_sha256=config_sha256,
            )
            failures.append(failure)
            counterexample = None
        if counterexample is not None:
            # Re-observe the final minimized value. Hypothesis may have last
            # evaluated a different shrink candidate before returning.
            reference, candidate, mismatches, status = observe(counterexample.table)
            if status is Status.PASSED:
                status = Status.ERROR
                mismatches = [
                    Mismatch(
                        kind=MismatchKind.EXCEPTION,
                        message=(
                            "the minimized witness was not reproducible; check callable state "
                            "and external dependencies"
                        ),
                        path="$campaign",
                    )
                ]
            failure = _example_result(
                counterexample.source, reference, candidate, mismatches, status
            )
            _store_failure(
                artifact_store,
                artifact_case,
                counterexample.table,
                failure,
                source=counterexample.source,
                seed=generation.seed,
                reference=reference_spec,
                candidate=candidate_spec,
                reference_observation=reference,
                candidate_observation=candidate,
                config_sha256=config_sha256,
            )
            failures.append(failure)

    performance: PerformanceResult | None = None
    semantics_passed = not failures
    if (
        not exact_only
        and semantics_passed
        and performance_config.enabled
        and benchmark is not None
        and representative is not None
    ):
        try:
            performance = benchmark(representative)
        except BenchmarkError as error:
            failures.append(
                ExampleResult(
                    source="performance",
                    status=Status.ERROR,
                    mismatches=[
                        Mismatch(
                            kind=MismatchKind.PERFORMANCE,
                            message=str(error),
                            path="$performance",
                        )
                    ],
                )
            )
        else:
            if performance.regression and performance_config.fail_on_regression:
                failures.append(_performance_failure(performance))

    mismatches = [mismatch for failure in failures for mismatch in failure.mismatches]
    status = _status_for(failures, None)
    verification = "captured"
    if exact_only and expected_provenance is None:
        verification = "unverified"
    elif expected_provenance is not None:
        verification = (
            "verified"
            if reference_runtime == expected_provenance.reference
            and candidate_runtime == expected_provenance.candidate
            else "drifted"
        )
    return CaseResult(
        name=name,
        status=status,
        examples_run=deterministic_count + generated_count,
        deterministic_examples=deterministic_count,
        generated_examples=generated_count,
        failures=failures,
        diagnoses=diagnose(mismatches),
        performance=performance,
        provenance=CaseProvenance(
            reference=reference_runtime,
            candidate=candidate_runtime,
            verification=verification,
        ),
        elapsed_seconds=time.perf_counter() - started,
    )


def _configured_case(
    case: CaseConfig,
    artifact_store: ArtifactStore,
    *,
    exact_only: bool = False,
    expected_provenance: CaseProvenance | None = None,
    config_sha256: str | None = None,
) -> CaseResult:
    configured_started = time.perf_counter()
    fixture = load_arrow_fixture(case.fixture) if case.fixture is not None else None
    schema = case.input_schema or (infer_schema(fixture) if fixture is not None else None)
    if schema is None:  # defensive; Pydantic normally prevents this state
        raise ValueError(f"case {case.name!r} has no fixture or schema")

    # A configured campaign amortizes interpreter/import startup across all
    # examples while retaining distinct reference and candidate process state.
    # Context-managed teardown also kills any surviving worker descendants.
    with (
        IsolatedExecutionSession(
            case.reference,
            timeout_seconds=case.timeout_seconds,
            expected_runtime=(expected_provenance.reference if expected_provenance else None),
        ) as reference_session,
        IsolatedExecutionSession(
            case.candidate,
            timeout_seconds=case.timeout_seconds,
            expected_runtime=(expected_provenance.candidate if expected_provenance else None),
        ) as candidate_session,
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="parity-pair") as pool,
    ):
        if expected_provenance is not None:
            reference_future = pool.submit(reference_session.inspect_runtime)
            candidate_future = pool.submit(candidate_session.inspect_runtime)
            reference_probe = reference_future.result()
            candidate_probe = candidate_future.result()
            provenance_mismatches: list[Mismatch] = []
            for label, expected, probe in (
                ("reference", expected_provenance.reference, reference_probe),
                ("candidate", expected_provenance.candidate, candidate_probe),
            ):
                if expected is None or probe.runtime is None or not probe.succeeded:
                    provenance_mismatches.append(
                        Mismatch(
                            kind=MismatchKind.EXCEPTION,
                            message=f"{label} runtime provenance could not be verified",
                            path=f"${label}.runtime",
                        )
                    )
                    continue
                if differences := diff_runtime(expected, probe.runtime):
                    provenance_mismatches.append(
                        Mismatch(
                            kind=MismatchKind.EXCEPTION,
                            message=(
                                f"{label} runtime provenance drifted ("
                                + ", ".join(differences)
                                + ")"
                            ),
                            path=f"${label}.runtime",
                        )
                    )
            if provenance_mismatches:
                failure = ExampleResult(
                    source="replay:provenance",
                    status=Status.ERROR,
                    mismatches=provenance_mismatches,
                    reference_metrics=reference_probe.metrics,
                    candidate_metrics=candidate_probe.metrics,
                )
                return CaseResult(
                    name=case.name,
                    status=Status.ERROR,
                    failures=[failure],
                    diagnoses=diagnose(provenance_mismatches),
                    provenance=CaseProvenance(
                        reference=reference_probe.runtime,
                        candidate=candidate_probe.runtime,
                        verification="drifted",
                    ),
                    elapsed_seconds=time.perf_counter() - configured_started,
                )

        def run(session: IsolatedExecutionSession, table: pa.Table) -> Observation:
            return session.execute(
                table,
                static_args=case.static_args,
                static_kwargs=case.static_kwargs,
            )

        def run_pair(table: pa.Table) -> tuple[Observation, Observation]:
            # Independent sessions make concurrent waits safe without sharing
            # callable globals or adapter arguments between the two sides.
            reference = pool.submit(run, reference_session, table)
            candidate = pool.submit(run, candidate_session, table)
            return reference.result(), candidate.result()

        return _campaign(
            name=case.name,
            schema=schema,
            fixture=fixture,
            comparison=case.comparison,
            generation=case.generation,
            performance_config=case.performance,
            artifact_store=artifact_store,
            reference_runner=lambda table: run(reference_session, table),
            candidate_runner=lambda table: run(candidate_session, table),
            pair_runner=run_pair,
            artifact_case=case,
            reference_spec=case.reference,
            candidate_spec=case.candidate,
            benchmark=lambda table: benchmark_observations(
                lambda: run(reference_session, table),
                lambda: run(candidate_session, table),
                case.performance,
            ),
            exact_only=exact_only,
            expected_provenance=expected_provenance,
            observed_provenance=(
                CaseProvenance(
                    reference=reference_probe.runtime,
                    candidate=candidate_probe.runtime,
                    verification="verified",
                )
                if expected_provenance is not None
                else None
            ),
            config_sha256=config_sha256,
        )


def _suite_status(cases: list[CaseResult]) -> Status:
    if any(case.status is Status.ERROR for case in cases):
        return Status.ERROR
    if any(case.status is Status.FAILED for case in cases):
        return Status.FAILED
    return Status.PASSED


def run_suite(
    config: ParityConfig,
    *,
    selected_cases: set[str] | None = None,
) -> SuiteResult:
    """Run a validated configuration and return a data-safe result graph."""

    started = time.perf_counter()
    known = {case.name for case in config.cases}
    if selected_cases is not None and (unknown := selected_cases - known):
        raise ValueError(f"unknown case(s): {', '.join(sorted(unknown))}")
    config_sha256 = effective_config_sha256(config, selected_cases=selected_cases)
    cases: list[CaseResult] = []
    store = ArtifactStore(config.artifact_dir)
    for case in config.cases:
        if selected_cases is not None and case.name not in selected_cases:
            continue
        try:
            result = _configured_case(case, store, config_sha256=config_sha256)
        except Exception as error:
            result = CaseResult(
                name=case.name,
                status=Status.ERROR,
                failures=[
                    ExampleResult(
                        source="campaign",
                        status=Status.ERROR,
                        mismatches=[
                            Mismatch(
                                kind=MismatchKind.EXCEPTION,
                                message=f"campaign could not run ({type(error).__name__})",
                                path="$campaign",
                            )
                        ],
                    )
                ],
                elapsed_seconds=0,
            )
        cases.append(result)
        if config.fail_fast and result.status is not Status.PASSED:
            break
    return SuiteResult(
        status=_suite_status(cases),
        cases=cases,
        elapsed_seconds=time.perf_counter() - started,
        parity_version=__version__,
        provenance=SuiteProvenance(
            orchestrator=collect_runtime_provenance(),
            config_sha256=config_sha256,
        ),
    )


def _importable_spec(
    function: Callable[..., Any],
    *,
    adapter: AdapterName,
    pandas_input: PandasInput = "arrow",
    record_distributions: Sequence[str] = (),
) -> CallableSpec | None:
    # Automatic replay is deliberately conservative: an import target can
    # reconstruct a module-level function, not a captured callable instance or
    # bound method. The exact identity check also rejects a stale, rebound or
    # monkeypatched function whose module path now names another object.
    if not inspect.isfunction(function):
        return None
    module = getattr(function, "__module__", "")
    qualified = getattr(function, "__qualname__", "")
    if not module or module == "__main__" or not qualified or "<locals>" in qualified:
        return None
    workdir: Path | None = None
    module_object = inspect.getmodule(function)
    if module_object is None:
        return None
    resolved: Any = module_object
    try:
        for part in qualified.split("."):
            resolved = getattr(resolved, part)
    except AttributeError:
        return None
    if resolved is not function:
        return None
    module_file = getattr(module_object, "__file__", None)
    if module_file:
        module_path = Path(module_file).resolve()
        workdir = module_path.parent
        package_depth = (
            len(module.split("."))
            if module_path.name == "__init__.py"
            else len(module.split(".")) - 1
        )
        for _ in range(package_depth):
            workdir = workdir.parent
    try:
        return CallableSpec(
            target=f"{module}:{qualified}",
            adapter=adapter,
            pandas_input=pandas_input,
            workdir=workdir,
            record_distributions=list(record_distributions),
        )
    except ValueError:
        return None


def run_live(
    reference: Callable[..., Any],
    candidate: Callable[..., Any],
    *,
    fixture: Any | None,
    schema: FrameSchema | None,
    comparison: ComparisonPolicy,
    generation: GenerationConfig,
    performance: PerformanceConfig,
    artifact_dir: Path,
    reference_adapter: AdapterName = "auto",
    candidate_adapter: AdapterName = "auto",
    reference_pandas_input: PandasInput = "arrow",
    candidate_pandas_input: PandasInput = "arrow",
    reference_distributions: Sequence[str] = (),
    candidate_distributions: Sequence[str] = (),
) -> SuiteResult:
    """Verify live callables in the current interpreter.

    Live mode cannot enforce a process timeout because local callables may not be
    importable. Configured campaigns should be used for isolation and CI gates.
    """

    started = time.perf_counter()
    # Validate and canonicalize explicit distribution names before either
    # callable runs. Otherwise two matching provenance-validation failures can
    # look like equivalent user exceptions and incorrectly pass the suite.
    reference_distributions = normalize_distribution_names(reference_distributions)
    candidate_distributions = normalize_distribution_names(candidate_distributions)
    # Fixture loaders and replay canonicalize chunk layout. Do the same before
    # live observation so a chunk-sensitive Arrow callable sees identical input
    # during the original run and replay.
    table = to_arrow(fixture).combine_chunks() if fixture is not None else None
    selected_schema = schema or (infer_schema(table) if table is not None else None)
    if selected_schema is None:
        raise ValueError("verify requires either fixture or schema")

    reference_spec = _importable_spec(
        reference,
        adapter=reference_adapter,
        pandas_input=reference_pandas_input,
        record_distributions=reference_distributions,
    )
    candidate_spec = _importable_spec(
        candidate,
        adapter=candidate_adapter,
        pandas_input=candidate_pandas_input,
        record_distributions=candidate_distributions,
    )

    artifact_case: str | CaseConfig = "live"
    if reference_spec is not None and candidate_spec is not None:
        artifact_case = CaseConfig(
            name="live",
            reference=reference_spec,
            candidate=candidate_spec,
            input_schema=selected_schema,
            comparison=comparison,
            generation=generation,
            performance=performance,
        )

    def reference_runner(value: pa.Table) -> Observation:
        return execute_callable_current(
            reference,
            value,
            adapter=reference_adapter,
            pandas_input=reference_pandas_input,
            record_distributions=reference_distributions,
        )

    def candidate_runner(value: pa.Table) -> Observation:
        return execute_callable_current(
            candidate,
            value,
            adapter=candidate_adapter,
            pandas_input=candidate_pandas_input,
            record_distributions=candidate_distributions,
        )

    live_contract = {
        "version": 1,
        "cases": [
            {
                "name": "live",
                "reference": (
                    reference_spec.model_dump(mode="python")
                    if reference_spec is not None
                    else {
                        "target": f"{reference.__module__}:{reference.__qualname__}",
                        "adapter": reference_adapter,
                        "pandas_input": reference_pandas_input,
                        "record_distributions": list(reference_distributions),
                    }
                ),
                "candidate": (
                    candidate_spec.model_dump(mode="python")
                    if candidate_spec is not None
                    else {
                        "target": f"{candidate.__module__}:{candidate.__qualname__}",
                        "adapter": candidate_adapter,
                        "pandas_input": candidate_pandas_input,
                        "record_distributions": list(candidate_distributions),
                    }
                ),
                "schema": selected_schema,
                "comparison": comparison,
                "generation": generation,
                "performance": performance,
            }
        ],
    }
    config_sha256 = effective_config_sha256(live_contract)
    result = _campaign(
        name="live",
        schema=selected_schema,
        fixture=table,
        comparison=comparison,
        generation=generation,
        performance_config=performance,
        artifact_store=ArtifactStore(artifact_dir),
        reference_runner=reference_runner,
        candidate_runner=candidate_runner,
        artifact_case=artifact_case,
        reference_spec=reference_spec,
        candidate_spec=candidate_spec,
        benchmark=lambda value: benchmark_observations(
            lambda: reference_runner(value),
            lambda: candidate_runner(value),
            performance,
        ),
        config_sha256=config_sha256,
    )
    return SuiteResult(
        status=result.status,
        cases=[result],
        elapsed_seconds=time.perf_counter() - started,
        parity_version=__version__,
        provenance=SuiteProvenance(
            orchestrator=collect_runtime_provenance(),
            config_sha256=config_sha256,
        ),
    )


def _artifact_root(path: Path) -> Path:
    if path.is_dir():
        return path
    if path.name in {"replay.json", "manifest.json"}:
        return path.parent
    raise ReplayError("artifact must be a campaign directory, replay.json, or manifest.json")


def _verify_manifest(root: Path) -> None:
    manifest_path = root / "manifest.json"
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplayError("artifact manifest is missing or invalid") from error
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ReplayError("unsupported artifact manifest")
    required = {"input.arrow", "replay.json", "result.json"}
    missing = required - set(manifest["files"])
    if missing:
        raise ReplayError(
            f"artifact manifest is missing required file(s): {', '.join(sorted(missing))}"
        )
    for name, metadata in manifest["files"].items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(metadata, dict):
            raise ReplayError("artifact manifest contains an unsafe file entry")
        file_path = root / name
        try:
            content = file_path.read_bytes()
        except OSError as error:
            raise ReplayError(f"artifact file is missing: {name}") from error
        if len(content) != metadata.get("bytes"):
            raise ReplayError(f"artifact size check failed: {name}")
        if hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
            raise ReplayError(f"artifact integrity check failed: {name}")


def _restore_environment(case_data: dict[str, Any]) -> None:
    for side in ("reference", "candidate"):
        spec = case_data.get(side)
        if not isinstance(spec, dict):
            raise ReplayError("live-callable artifacts cannot be replayed automatically")
        required = spec.get("environment", {})
        if not isinstance(required, dict):
            raise ReplayError("invalid replay environment declaration")
        missing = [key for key in required if key not in os.environ]
        if missing:
            raise ReplayError(
                f"replay requires {len(missing)} environment variable(s) that are not set"
            )
        spec["environment"] = {key: os.environ[key] for key in required}


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return "<redacted>" in value or "<path>" in value
    if isinstance(value, dict):
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redaction(item) for item in value)
    return False


def _resolve_replay_paths(case_data: dict[str, Any], invocation_cwd: Path) -> None:
    """Resolve sanitized paths relative to the replay invocation directory."""

    base = invocation_cwd.resolve()
    for side in ("reference", "candidate"):
        spec = case_data.get(side)
        if not isinstance(spec, dict):
            raise ReplayError("live-callable artifacts cannot be replayed automatically")
        for field in ("workdir", "python"):
            raw = spec.get(field)
            if raw is None:
                if field == "workdir":
                    spec[field] = base
                continue
            if not isinstance(raw, str):
                raise ReplayError(f"invalid replay {field} declaration")
            relative = Path(raw)
            if relative.is_absolute():
                raise ReplayError(f"replay {field} paths must be relative")
            resolved = (base / relative).resolve()
            if not resolved.is_relative_to(base):
                raise ReplayError(f"replay {field} paths must stay inside the invocation directory")
            spec[field] = resolved


def replay_artifact(path: str | Path) -> SuiteResult:
    """Validate an artifact manifest and re-run its exact saved input."""

    started = time.perf_counter()
    root = _artifact_root(Path(path).resolve())
    _verify_manifest(root)
    try:
        replay: dict[str, Any] = json.loads((root / "replay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplayError("replay.json is missing or invalid") from error
    replay_version = replay.get("version")
    if replay_version not in {1, 2} or not isinstance(replay.get("case"), dict):
        raise ReplayError("unsupported replay contract")
    if replay.get("path_base") != "invocation_cwd":
        raise ReplayError("unsupported replay path base")
    if replay.get("input") != "input.arrow":
        raise ReplayError("unsupported replay input")
    expected_provenance: CaseProvenance | None = None
    config_sha256: str | None = None
    if replay_version == 2:
        try:
            expected_provenance = CaseProvenance.model_validate(replay.get("expected_runtime"))
        except ValueError as error:
            raise ReplayError("replay runtime provenance is missing or invalid") from error
        if expected_provenance.reference is None or expected_provenance.candidate is None:
            raise ReplayError("replay runtime provenance is incomplete")
        raw_config_sha256 = replay.get("config_sha256")
        if not isinstance(raw_config_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", raw_config_sha256
        ):
            raise ReplayError("replay configuration fingerprint is missing or invalid")
        config_sha256 = raw_config_sha256
    case_data = dict(replay["case"])
    if _contains_redaction(case_data.get("static_args")) or _contains_redaction(
        case_data.get("static_kwargs")
    ):
        raise ReplayError("redacted static arguments cannot be replayed automatically")
    _restore_environment(case_data)
    _resolve_replay_paths(case_data, Path.cwd().resolve())
    case_data["fixture"] = root / "input.arrow"
    try:
        case = CaseConfig.model_validate(case_data)
    except ValueError as error:
        raise ReplayError("artifact contains an invalid case configuration") from error
    # A replay verifies existing evidence; it must not require the artifact
    # mount itself to be writable. Any transient re-observation evidence stays
    # in a private temporary directory, while reports point back to the source.
    with tempfile.TemporaryDirectory(prefix="parity-replay-") as temporary:
        result = _configured_case(
            case,
            ArtifactStore(Path(temporary) / "artifacts"),
            exact_only=True,
            expected_provenance=expected_provenance,
            config_sha256=config_sha256,
        )
    for failure in result.failures:
        failure.artifact = root
    return SuiteResult(
        status=result.status,
        cases=[result],
        elapsed_seconds=time.perf_counter() - started,
        parity_version=__version__,
        provenance=SuiteProvenance(
            orchestrator=collect_runtime_provenance(),
            config_sha256=config_sha256,
        ),
    )


__all__ = ["ReplayError", "replay_artifact", "run_live", "run_suite"]
