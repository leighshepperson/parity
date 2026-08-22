"""Campaign orchestration for semantic and performance verification.

The engine is intentionally small enough to audit.  It coordinates the public
generation, execution, comparison, diagnosis, artifact, and reporting layers;
none of those layers decides the outcome on its behalf.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import keyword
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal, TypeAlias

import pyarrow as pa

from parity._version import __version__
from parity.adapters import load_arrow_fixture, to_arrow
from parity.artifacts import ArtifactStore
from parity.comparison import compare_observations, mismatch_signature
from parity.custom_generation import CustomGenerator, load_custom_generator
from parity.diagnostics import diagnose
from parity.execution import (
    ArrowInputBundle,
    ExecutionOutcome,
    IsolatedExecutionSession,
    Observation,
    execute_callable_current,
)
from parity.generation import (
    adversarial_bundle_cases,
    adversarial_cases,
)
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
    InputBundle,
    InputSpec,
    JsonValue,
    Mismatch,
    MismatchKind,
    PandasInput,
    ParityConfig,
    PerformanceConfig,
    PerformanceResult,
    Relationship,
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
from parity.schema import infer_schema, rows_satisfy_frame_constraints, validate_bundle_schemas
from parity.shrinking import (
    find_unseen_bundle_counterexample,
    find_unseen_counterexample,
    find_unseen_custom_counterexample,
)


class ReplayError(ValueError):
    """Raised when an artifact cannot be safely or exactly replayed."""


CampaignInput: TypeAlias = pa.Table | dict[str, pa.Table]
Runner = Callable[[CampaignInput], Observation]
PairRunner = Callable[[CampaignInput], tuple[Observation, Observation]]


# Repeat observations answer a different question from cross-side comparison:
# did this implementation return the same canonical result twice?  Keep that
# identity reflexive even when a user deliberately configures null/null or
# NaN/NaN as cross-side contract failures, and make it exact rather than
# inheriting tolerances, ignored columns, or relaxed ordering from that policy.
_STABILITY_IDENTITY_POLICY = ComparisonPolicy(
    column_order="strict",
    row_order="strict",
    row_keys=[],
    dtype="strict",
    names="strict",
    null_equal=True,
    nan_equal=True,
    null_nan_equal=False,
    signed_zero_equal=False,
    check_exceptions=True,
    check_input_mutation=True,
    rtol=0.0,
    atol=0.0,
    datetime_tolerance_ns=0,
    ignored_columns=[],
)


def _observation_changed(initial: Observation, repeated: Observation) -> bool:
    return bool(compare_observations(initial, repeated, _STABILITY_IDENTITY_POLICY))


class _StopGeneratedCampaign(BaseException):
    """Control-flow signal preventing Hypothesis from shrinking operational errors."""


def _infrastructure_error(observation: Observation) -> bool:
    # Infrastructure is an explicit execution outcome. Never infer it from an
    # exception's class or module: a target is allowed to raise TimeoutError,
    # ExecutionError, or any other exception as observable domain behaviour.
    return observation.outcome in {
        ExecutionOutcome.ERROR,
        ExecutionOutcome.CRASHED,
        ExecutionOutcome.TIMED_OUT,
    }


def _infrastructure_mismatch(label: str, observation: Observation) -> Mismatch:
    exception = observation.exception
    kind = exception.type if exception else observation.outcome.value
    return Mismatch(
        kind=MismatchKind.EXCEPTION,
        message=f"{label} could not be executed ({kind})",
        path=f"${label}",
    )


def _observe_pair(
    value: CampaignInput,
    reference_runner: Runner,
    candidate_runner: Runner,
    policy: ComparisonPolicy,
    pair_runner: PairRunner | None = None,
) -> tuple[Observation, Observation, list[Mismatch], Status]:
    if pair_runner is None:
        reference = reference_runner(value)
        candidate = candidate_runner(value)
    else:
        reference, candidate = pair_runner(value)
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
        finding_signature=(
            mismatch_signature(mismatches) if status is Status.FAILED and mismatches else None
        ),
    )


def _store_failure(
    store: ArtifactStore,
    case: str | CaseConfig,
    value: CampaignInput,
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
        value,
        result,
        reference=reference,
        candidate=candidate,
        source=source,
        seed=seed,
        runtime_provenance=CaseProvenance(
            reference=reference_observation.runtime,
            candidate=candidate_observation.runtime,
        ),
        reference_observation=reference_observation,
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


def _input_row_count(value: CampaignInput) -> int:
    """Return a stable size hint for representative-input selection."""

    if isinstance(value, pa.Table):
        return int(value.num_rows)
    return sum(table.num_rows for table in value.values())


def _campaign_error(
    message: str,
    reference: Observation,
    candidate: Observation,
    *,
    source: str = "generated:unstable",
) -> ExampleResult:
    """Describe a failure of the discovery process, not a semantic finding."""

    return ExampleResult(
        source=source,
        status=Status.ERROR,
        mismatches=[
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message=message,
                path="$campaign",
            )
        ],
        reference_metrics=reference.metrics,
        candidate_metrics=candidate.metrics,
    )


def _stability_error(
    repeat: int,
    reference: Observation,
    candidate: Observation,
    *,
    reference_changed: bool,
    candidate_changed: bool,
) -> ExampleResult:
    """Describe same-input drift without exposing either observed value."""

    changed = [
        label
        for label, differs in (
            ("reference", reference_changed),
            ("candidate", candidate_changed),
        )
        if differs
    ]
    if not changed:
        # A pair can cross a configured tolerance boundary even when each
        # repeat remains individually close to its baseline. The comparison
        # result still changed, but neither side can be attributed safely.
        changed = ["reference-candidate-pair"]
    mismatches = [
        Mismatch(
            kind=MismatchKind.EXCEPTION,
            message=f"{label.replace('-', ' ')} changed on stability repeat {repeat}",
            path=(
                f"${label}.stability[{repeat}]"
                if label in {"reference", "candidate"}
                else f"$campaign.stability[{repeat}]"
            ),
        )
        for label in changed
    ]
    return ExampleResult(
        source=f"deterministic:stability:{','.join(changed)}:repeat-{repeat}",
        status=Status.ERROR,
        mismatches=mismatches,
        reference_metrics=reference.metrics,
        candidate_metrics=candidate.metrics,
    )


def _campaign(
    *,
    name: str,
    schema: FrameSchema | None,
    fixture: CampaignInput | None,
    input_bundle: InputBundle | None = None,
    bundle_schemas: Mapping[str, FrameSchema] | None = None,
    custom_generator: CustomGenerator | None = None,
    comparison: ComparisonPolicy,
    generation: GenerationConfig,
    performance_config: PerformanceConfig,
    artifact_store: ArtifactStore,
    reference_runner: Runner,
    candidate_runner: Runner,
    pair_runner: PairRunner | None = None,
    confirmation_pair_runner: PairRunner | None = None,
    artifact_case: str | CaseConfig,
    reference_spec: CallableSpec | None = None,
    candidate_spec: CallableSpec | None = None,
    benchmark: Callable[[CampaignInput], PerformanceResult] | None = None,
    exact_only: bool = False,
    expected_provenance: CaseProvenance | None = None,
    observed_provenance: CaseProvenance | None = None,
    config_sha256: str | None = None,
) -> CaseResult:
    started = time.perf_counter()
    failures: list[ExampleResult] = []
    deterministic_count = 0
    generated_count = 0
    representative: CampaignInput | None = None
    reference_runtime = observed_provenance.reference if observed_provenance else None
    candidate_runtime = observed_provenance.candidate if observed_provenance else None
    seen_signatures: set[str] = set()
    operational_error = False

    has_single_contract = schema is not None
    has_bundle_contract = input_bundle is not None and bundle_schemas is not None
    has_custom_contract = custom_generator is not None
    if sum((has_single_contract, has_bundle_contract, has_custom_contract)) != 1:
        raise ValueError(
            "campaign requires exactly one single-frame, input-bundle, or custom-generator contract"
        )

    # Frame and relationship constraints define the valid domain even when
    # deterministic edge families are disabled. Validate explicit fixtures
    # here so the adversarial toggle cannot silently bypass that contract.
    if isinstance(fixture, pa.Table) and schema is not None:
        if not rows_satisfy_frame_constraints(schema, fixture.to_pylist()):
            raise ValueError("fixture does not satisfy the declared frame constraints")
    elif isinstance(fixture, dict) and input_bundle is not None and bundle_schemas is not None:
        adversarial_bundle_cases(input_bundle, bundle_schemas, fixtures=fixture)

    def observe(value: CampaignInput) -> tuple[Observation, Observation, list[Mismatch], Status]:
        nonlocal reference_runtime, candidate_runtime
        observed = _observe_pair(
            value,
            reference_runner,
            candidate_runner,
            comparison,
            pair_runner,
        )
        reference, candidate = observed[:2]
        reference_runtime = reference_runtime or reference.runtime
        candidate_runtime = candidate_runtime or candidate.runtime
        return observed

    def confirm(value: CampaignInput) -> tuple[Observation, Observation, list[Mismatch], Status]:
        """Re-observe a witness, using clean execution state when available."""

        if confirmation_pair_runner is None:
            return observe(value)
        return _observe_pair(
            value,
            reference_runner,
            candidate_runner,
            comparison,
            confirmation_pair_runner,
        )

    def remember_representative(value: CampaignInput) -> None:
        """Retain the largest validated input observed by this campaign."""

        nonlocal representative
        if representative is None or _input_row_count(value) > _input_row_count(representative):
            # Arrow tables are immutable. Copy only the bundle container so a
            # custom strategy cannot later rebind one of its logical inputs.
            representative = value if isinstance(value, pa.Table) else dict(value)

    deterministic: list[tuple[str, CampaignInput]]
    if exact_only:
        if fixture is None:
            raise ReplayError("the replay artifact has no input fixture")
        deterministic = [("replay", fixture)]
    elif custom_generator is not None:
        deterministic = [
            (f"generated:custom:{index}", value)
            for index, value in enumerate(custom_generator.examples, start=1)
        ]
    elif input_bundle is not None and bundle_schemas is not None:
        if generation.adversarial_examples:
            fixtures = fixture if isinstance(fixture, dict) else None
            deterministic = [
                (generated.source, generated.tables)
                for generated in adversarial_bundle_cases(
                    input_bundle,
                    bundle_schemas,
                    fixtures=fixtures,
                )
            ]
        elif fixture is not None:
            deterministic = [("fixture", fixture)]
        else:
            deterministic = []
    elif generation.adversarial_examples and schema is not None:
        single_fixture = fixture if isinstance(fixture, pa.Table) else None
        deterministic = [
            (generated.source, generated.table)
            for generated in adversarial_cases(schema, fixture=single_fixture)
        ]
    elif fixture is not None:
        deterministic = [("fixture", fixture)]
    else:
        deterministic = []

    if not exact_only and not generation.search and not deterministic:
        raise ValueError("generation.search=false requires at least one deterministic input")

    for source, value in deterministic:
        if source.startswith("generated:custom:"):
            generated_count += 1
        else:
            deterministic_count += 1
        reference, candidate, mismatches, status = observe(value)
        if status is Status.ERROR:
            failure = _example_result(source, reference, candidate, mismatches, status)
            _store_failure(
                artifact_store,
                artifact_case,
                value,
                failure,
                source=source,
                seed=generation.seed,
                reference=reference_spec,
                candidate=candidate_spec,
                reference_observation=reference,
                candidate_observation=candidate,
                config_sha256=config_sha256,
            )
            failures.append(failure)
            operational_error = True
            break
        if status is Status.PASSED:
            # Successful pairwise comparison alone cannot establish that either
            # callable is stable: synchronized call counters or random streams
            # can change together and remain pairwise equal. Re-observe only
            # deterministic inputs, immediately and in the same sessions. The
            # executor supplies a fresh adapter argument for every invocation.
            for repeat in range(2, generation.stability_repeats + 1):
                repeated_reference, repeated_candidate, _, repeated_status = observe(value)
                reference_changed = _observation_changed(reference, repeated_reference)
                candidate_changed = _observation_changed(candidate, repeated_candidate)
                if (
                    repeated_status is Status.PASSED
                    and not reference_changed
                    and not candidate_changed
                ):
                    continue
                failure = _stability_error(
                    repeat,
                    repeated_reference,
                    repeated_candidate,
                    reference_changed=reference_changed,
                    candidate_changed=candidate_changed,
                )
                _store_failure(
                    artifact_store,
                    artifact_case,
                    value,
                    failure,
                    source=failure.source,
                    seed=generation.seed,
                    reference=reference_spec,
                    candidate=candidate_spec,
                    reference_observation=repeated_reference,
                    candidate_observation=repeated_candidate,
                    config_sha256=config_sha256,
                )
                failures.append(failure)
                operational_error = True
                break
            if operational_error:
                break
            remember_representative(value)
        if status is Status.FAILED:
            if exact_only:
                failure = _example_result(source, reference, candidate, mismatches, status)
                _store_failure(
                    artifact_store,
                    artifact_case,
                    value,
                    failure,
                    source=source,
                    seed=generation.seed,
                    reference=reference_spec,
                    candidate=candidate_spec,
                    reference_observation=reference,
                    candidate_observation=candidate,
                    config_sha256=config_sha256,
                )
                failures.append(failure)
                break
            initial_signature = mismatch_signature(mismatches)
            repeated_reference, repeated_candidate, repeated_mismatches, repeated_status = confirm(
                value
            )
            deterministic_error: str | None = None
            if repeated_status is Status.ERROR:
                failure = _example_result(
                    source,
                    repeated_reference,
                    repeated_candidate,
                    repeated_mismatches,
                    Status.ERROR,
                )
            elif repeated_status is Status.PASSED:
                deterministic_error = (
                    "the deterministic witness was not reproducible; check callable state "
                    "and external dependencies"
                )
            elif mismatch_signature(repeated_mismatches) != initial_signature:
                deterministic_error = (
                    "the deterministic witness produced different mismatch signatures "
                    "across repeated evaluation"
                )
            else:
                unstable_sides = [
                    label
                    for label, differences in (
                        (
                            "reference",
                            _observation_changed(reference, repeated_reference),
                        ),
                        (
                            "candidate",
                            _observation_changed(candidate, repeated_candidate),
                        ),
                    )
                    if differences
                ]
                if unstable_sides:
                    deterministic_error = (
                        "the deterministic witness was nondeterministic on the "
                        + ", ".join(unstable_sides)
                        + " side"
                    )

            if deterministic_error is not None:
                failure = _campaign_error(
                    deterministic_error,
                    repeated_reference,
                    repeated_candidate,
                    source="deterministic:unstable",
                )
            elif repeated_status is Status.FAILED:
                failure = _example_result(
                    source,
                    repeated_reference,
                    repeated_candidate,
                    repeated_mismatches,
                    Status.FAILED,
                )

            if failure.status is Status.ERROR:
                _store_failure(
                    artifact_store,
                    artifact_case,
                    value,
                    failure,
                    source=failure.source,
                    seed=generation.seed,
                    reference=reference_spec,
                    candidate=candidate_spec,
                    reference_observation=repeated_reference,
                    candidate_observation=repeated_candidate,
                    config_sha256=config_sha256,
                )
                failures.append(failure)
                operational_error = True
                break

            signature = failure.finding_signature
            if signature is None:  # pragma: no cover - enforced by _example_result
                raise RuntimeError("semantic failure has no finding signature")
            if signature not in seen_signatures:
                _store_failure(
                    artifact_store,
                    artifact_case,
                    value,
                    failure,
                    source=source,
                    seed=generation.seed,
                    reference=reference_spec,
                    candidate=candidate_spec,
                    reference_observation=repeated_reference,
                    candidate_observation=repeated_candidate,
                    config_sha256=config_sha256,
                )
                failures.append(failure)
                seen_signatures.add(signature)
            if len(seen_signatures) >= generation.max_findings:
                break

    # Each search receives its own bounded Hypothesis budget. A classifier
    # excludes already confirmed signatures, so max_findings bounds both the
    # amount of evidence and the worst-case search cost.
    while (
        generation.search
        and not exact_only
        and not operational_error
        and len(seen_signatures) < generation.max_findings
        and (custom_generator is None or custom_generator.uses_hypothesis)
    ):
        latest: tuple[Observation, Observation, list[Mismatch], Status] | None = None
        latest_value: CampaignInput | None = None

        def classify(value: CampaignInput) -> str | None:
            nonlocal generated_count, latest, latest_value
            generated_count += 1
            latest_value = value
            latest = observe(value)
            if latest[3] is Status.ERROR:
                # Operational failures are not a semantic property of input.
                # BaseException prevents Hypothesis from retrying or shrinking.
                raise _StopGeneratedCampaign
            if latest[3] is Status.PASSED:
                remember_representative(value)
                return None
            return mismatch_signature(latest[2])

        try:
            if custom_generator is not None and custom_generator.strategy is not None:
                custom_counterexample = find_unseen_custom_counterexample(
                    custom_generator.strategy,
                    classify,
                    seen_signatures,
                    generation,
                )
                counterexample_value = (
                    custom_counterexample.example if custom_counterexample is not None else None
                )
                counterexample_source = (
                    custom_counterexample.source if custom_counterexample is not None else None
                )
            elif input_bundle is not None and bundle_schemas is not None:
                counterexample = find_unseen_bundle_counterexample(
                    input_bundle,
                    bundle_schemas,
                    classify,
                    seen_signatures,
                    generation,
                )
                counterexample_value = counterexample.tables if counterexample is not None else None
                counterexample_source = (
                    counterexample.source if counterexample is not None else None
                )
            elif schema is not None:
                single_counterexample = find_unseen_counterexample(
                    schema,
                    classify,
                    seen_signatures,
                    generation,
                )
                counterexample_value = (
                    single_counterexample.table if single_counterexample is not None else None
                )
                counterexample_source = (
                    single_counterexample.source if single_counterexample is not None else None
                )
            else:  # pragma: no cover - guarded at campaign entry
                raise RuntimeError("campaign input contract is missing")
        except _StopGeneratedCampaign:
            if latest is None or latest_value is None:  # pragma: no cover - defensive
                raise
            reference, candidate, mismatches, status = latest
            failure = _example_result("generated:error", reference, candidate, mismatches, status)
            _store_failure(
                artifact_store,
                artifact_case,
                latest_value,
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
            operational_error = True
            break
        if counterexample_value is None or counterexample_source is None:
            break

        # Hypothesis may finish on a different shrink attempt than the returned
        # value. Confirm twice, and require each implementation to be stable on
        # its own before calling the witness a distinct semantic finding.
        first = confirm(counterexample_value)
        second = confirm(counterexample_value)
        generated_count += 2
        first_reference, first_candidate, first_mismatches, first_status = first
        second_reference, second_candidate, second_mismatches, second_status = second
        failure_reference = second_reference
        failure_candidate = second_candidate

        confirmation_message: str | None = None
        if first_status is Status.ERROR or second_status is Status.ERROR:
            error_observation = first if first_status is Status.ERROR else second
            error_reference, error_candidate, error_mismatches, _ = error_observation
            failure_reference = error_reference
            failure_candidate = error_candidate
            failure = _example_result(
                "generated:error",
                error_reference,
                error_candidate,
                error_mismatches,
                Status.ERROR,
            )
        elif first_status is Status.PASSED or second_status is Status.PASSED:
            confirmation_message = (
                "the minimized witness was not reproducible; check callable state "
                "and external dependencies"
            )
            failure = _campaign_error(
                confirmation_message,
                second_reference,
                second_candidate,
            )
        else:
            first_signature = mismatch_signature(first_mismatches)
            second_signature = mismatch_signature(second_mismatches)
            reference_instability = _observation_changed(first_reference, second_reference)
            candidate_instability = _observation_changed(first_candidate, second_candidate)
            if first_signature != second_signature:
                confirmation_message = (
                    "the minimized witness produced different mismatch signatures "
                    "across repeated evaluation"
                )
            elif reference_instability or candidate_instability:
                unstable_side_names = ", ".join(
                    label
                    for label, differences in (
                        ("reference", reference_instability),
                        ("candidate", candidate_instability),
                    )
                    if differences
                )
                confirmation_message = (
                    f"the minimized witness was nondeterministic on the {unstable_side_names} side"
                )
            elif first_signature in seen_signatures:
                confirmation_message = (
                    "the minimized witness changed into an already recorded mismatch signature"
                )

            if confirmation_message is not None:
                failure = _campaign_error(
                    confirmation_message,
                    second_reference,
                    second_candidate,
                )
            else:
                failure = _example_result(
                    counterexample_source,
                    second_reference,
                    second_candidate,
                    second_mismatches,
                    Status.FAILED,
                )

        if failure.status is Status.ERROR:
            _store_failure(
                artifact_store,
                artifact_case,
                counterexample_value,
                failure,
                source=failure.source,
                seed=generation.seed,
                reference=reference_spec,
                candidate=candidate_spec,
                reference_observation=failure_reference,
                candidate_observation=failure_candidate,
                config_sha256=config_sha256,
            )
            failures.append(failure)
            operational_error = True
            break

        signature = failure.finding_signature
        if signature is None:  # pragma: no cover - enforced by _example_result
            raise RuntimeError("confirmed semantic failure has no finding signature")
        _store_failure(
            artifact_store,
            artifact_case,
            counterexample_value,
            failure,
            source=counterexample_source,
            seed=generation.seed,
            reference=reference_spec,
            candidate=candidate_spec,
            reference_observation=second_reference,
            candidate_observation=second_candidate,
            config_sha256=config_sha256,
        )
        failures.append(failure)
        seen_signatures.add(signature)

    performance: PerformanceResult | None = None
    semantics_passed = not failures
    if not exact_only and semantics_passed and performance_config.enabled:
        if benchmark is None or representative is None:
            failures.append(
                ExampleResult(
                    source="performance",
                    status=Status.ERROR,
                    mismatches=[
                        Mismatch(
                            kind=MismatchKind.PERFORMANCE,
                            message=(
                                "performance measurement runner is unavailable"
                                if benchmark is None
                                else "performance measurement has no validated representative input"
                            ),
                            path="$performance",
                        )
                    ],
                )
            )
        else:
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

    semantic_failures = sorted(
        (failure for failure in failures if failure.finding_signature is not None),
        key=lambda failure: failure.finding_signature or "",
    )
    nonsemantic_failures = [failure for failure in failures if failure.finding_signature is None]
    failures = [*semantic_failures, *nonsemantic_failures]
    diagnostic_mismatches = [
        mismatch
        for failure in failures
        if failure.status is Status.FAILED
        for mismatch in failure.mismatches
    ]
    status = _status_for(failures, None)
    diagnoses = diagnose(diagnostic_mismatches) if status is Status.FAILED else []
    verification = "captured"
    if expected_provenance is not None:
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
        finding_limit_reached=(not exact_only and len(seen_signatures) >= generation.max_findings),
        failures=failures,
        diagnoses=diagnoses,
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
    input_bundle = case.input_bundle
    bundle_schemas: dict[str, FrameSchema] | None = None
    custom_generator: CustomGenerator | None = None
    fixture: CampaignInput | None
    schema: FrameSchema | None
    if case.generation.generator is not None:
        custom_generator = load_custom_generator(
            case.generation.generator,
            base_directory=case._base_directory,
            max_examples=case.generation.max_examples,
        )
        fixture = None
        schema = None
    elif input_bundle is None:
        fixture = load_arrow_fixture(case.fixture) if case.fixture is not None else None
        schema = case.input_schema or (
            infer_schema(fixture) if isinstance(fixture, pa.Table) else None
        )
        if schema is None:  # defensive; Pydantic normally prevents this state
            raise ValueError(f"case {case.name!r} has no fixture or schema")
    else:
        loaded_fixtures: dict[str, pa.Table] = {}
        bundle_schemas = {}
        for input_name, input_spec in input_bundle.inputs.items():
            input_fixture = (
                load_arrow_fixture(input_spec.fixture) if input_spec.fixture is not None else None
            )
            if input_fixture is not None:
                loaded_fixtures[input_name] = input_fixture
            resolved_schema = input_spec.input_schema or (
                infer_schema(input_fixture) if input_fixture is not None else None
            )
            if resolved_schema is None:  # pragma: no cover - model validation is defensive
                raise ValueError(
                    f"case {case.name!r} input {input_name!r} has no fixture or schema"
                )
            bundle_schemas[input_name] = resolved_schema
        validate_bundle_schemas(input_bundle, bundle_schemas)
        # A deterministic fixture must be an atomic bundle. Partial fixture sets
        # still inform schema inference but are never mixed with generated peers.
        fixture = loaded_fixtures if len(loaded_fixtures) == len(input_bundle.inputs) else None
        schema = None

    def bound_input(value: CampaignInput) -> ArrowInputBundle:
        if isinstance(value, pa.Table):
            return value
        if input_bundle is None or input_bundle.binding == "keyword":
            return value
        return tuple(value[name] for name in input_bundle.inputs)

    reference_kwargs = {**case.static_kwargs, **case.reference_kwargs}
    candidate_kwargs = {**case.static_kwargs, **case.candidate_kwargs}

    def run_clean_pair(value: CampaignInput) -> tuple[Observation, Observation]:
        """Execute one confirmation in newly started reference/candidate workers."""

        with (
            IsolatedExecutionSession(
                case.reference,
                timeout_seconds=case.timeout_seconds,
                expected_runtime=(expected_provenance.reference if expected_provenance else None),
            ) as clean_reference,
            IsolatedExecutionSession(
                case.candidate,
                timeout_seconds=case.timeout_seconds,
                expected_runtime=(expected_provenance.candidate if expected_provenance else None),
            ) as clean_candidate,
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="parity-confirm") as clean_pool,
        ):
            reference_future = clean_pool.submit(
                clean_reference.execute,
                bound_input(value),
                static_args=case.static_args,
                static_kwargs=reference_kwargs,
            )
            candidate_future = clean_pool.submit(
                clean_candidate.execute,
                bound_input(value),
                static_args=case.static_args,
                static_kwargs=candidate_kwargs,
            )
            return reference_future.result(), candidate_future.result()

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

        def preflight_mismatches(
            reference_probe: Observation,
            candidate_probe: Observation,
        ) -> list[Mismatch]:
            mismatches: list[Mismatch] = []
            for label, expected, probe in (
                (
                    "reference",
                    expected_provenance.reference if expected_provenance else None,
                    reference_probe,
                ),
                (
                    "candidate",
                    expected_provenance.candidate if expected_provenance else None,
                    candidate_probe,
                ),
            ):
                if probe.runtime is None or not probe.succeeded:
                    exception = probe.exception
                    exception_type = exception.type if exception is not None else None
                    if exception is not None and exception_type == "RuntimeContractError":
                        detail = exception.message
                        component = "runtime"
                    elif exception_type == "TargetEndpointError":
                        detail = "endpoint preflight could not be completed"
                        component = "endpoint"
                    elif exception_type == "TargetTransportError":
                        detail = "transport preflight could not be completed"
                        component = "transport"
                    else:
                        detail = "runtime provenance could not be verified"
                        component = "runtime"
                    mismatches.append(
                        Mismatch(
                            kind=MismatchKind.EXCEPTION,
                            message=f"{label} {detail}",
                            path=f"${label}.{component}",
                        )
                    )
                    continue
                if expected is not None and (differences := diff_runtime(expected, probe.runtime)):
                    mismatches.append(
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
            return mismatches

        def preflight_error(
            mismatches: list[Mismatch],
            reference_probe: Observation,
            candidate_probe: Observation,
        ) -> CaseResult:
            failure = ExampleResult(
                source=("replay:provenance" if expected_provenance else "runtime:preflight"),
                status=Status.ERROR,
                mismatches=mismatches,
                reference_metrics=reference_probe.metrics,
                candidate_metrics=candidate_probe.metrics,
            )
            return CaseResult(
                name=case.name,
                status=Status.ERROR,
                failures=[failure],
                diagnoses=[],
                provenance=CaseProvenance(
                    reference=reference_probe.runtime,
                    candidate=candidate_probe.runtime,
                    verification="drifted",
                ),
                elapsed_seconds=time.perf_counter() - configured_started,
            )

        # Validate both runtimes before importing either endpoint. A drift or
        # unmet dependency on one side must not execute import-time code on the
        # other side during replay or setup failure.
        reference_future = pool.submit(reference_session.preflight_transport)
        candidate_future = pool.submit(candidate_session.preflight_transport)
        reference_probe = reference_future.result()
        candidate_probe = candidate_future.result()
        if provenance_mismatches := preflight_mismatches(reference_probe, candidate_probe):
            return preflight_error(provenance_mismatches, reference_probe, candidate_probe)

        reference_future = pool.submit(reference_session.preflight_endpoint)
        candidate_future = pool.submit(candidate_session.preflight_endpoint)
        reference_probe = reference_future.result()
        candidate_probe = candidate_future.result()
        if endpoint_mismatches := preflight_mismatches(reference_probe, candidate_probe):
            return preflight_error(endpoint_mismatches, reference_probe, candidate_probe)

        def run(
            session: IsolatedExecutionSession,
            value: CampaignInput,
            endpoint_kwargs: Mapping[str, JsonValue],
        ) -> Observation:
            return session.execute(
                bound_input(value),
                static_args=case.static_args,
                static_kwargs=endpoint_kwargs,
            )

        def run_pair(value: CampaignInput) -> tuple[Observation, Observation]:
            # Independent sessions make concurrent waits safe without sharing
            # callable globals or adapter arguments between the two sides.
            reference = pool.submit(run, reference_session, value, reference_kwargs)
            candidate = pool.submit(run, candidate_session, value, candidate_kwargs)
            return reference.result(), candidate.result()

        return _campaign(
            name=case.name,
            schema=schema,
            fixture=fixture,
            input_bundle=input_bundle,
            bundle_schemas=bundle_schemas,
            custom_generator=custom_generator,
            comparison=case.comparison,
            generation=case.generation,
            performance_config=case.performance,
            artifact_store=artifact_store,
            reference_runner=lambda value: run(reference_session, value, reference_kwargs),
            candidate_runner=lambda value: run(candidate_session, value, candidate_kwargs),
            pair_runner=run_pair,
            confirmation_pair_runner=run_clean_pair,
            artifact_case=case,
            reference_spec=case.reference,
            candidate_spec=case.candidate,
            benchmark=lambda value: benchmark_observations(
                lambda: run(reference_session, value, reference_kwargs),
                lambda: run(candidate_session, value, candidate_kwargs),
                case.performance,
            ),
            exact_only=exact_only,
            expected_provenance=expected_provenance,
            observed_provenance=CaseProvenance(
                reference=reference_probe.runtime,
                candidate=candidate_probe.runtime,
                verification="verified" if expected_provenance is not None else "captured",
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
    config_sha256 = effective_config_sha256(
        config,
        selected_cases=selected_cases,
        base_directory=config._base_directory,
    )
    selected = [
        case for case in config.cases if selected_cases is None or case.name in selected_cases
    ]

    def configured_case(case: CaseConfig) -> CaseResult:
        effective_case = case.model_copy(deep=True)
        if config.native_threads is not None:
            if effective_case.reference.native_threads is None:
                effective_case.reference.native_threads = config.native_threads
            if effective_case.candidate.native_threads is None:
                effective_case.candidate.native_threads = config.native_threads
        try:
            return _configured_case(
                effective_case,
                ArtifactStore(
                    config.artifact_dir,
                    invocation_directory=config._base_directory or Path.cwd(),
                ),
                config_sha256=config_sha256,
            )
        except Exception as error:
            return CaseResult(
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

    cases: list[CaseResult]
    if not selected:
        cases = []
    elif config.jobs == 1:
        cases = []
        for case in selected:
            result = configured_case(case)
            cases.append(result)
            if config.fail_fast and result.status is not Status.PASSED:
                break
    else:
        ordered: list[CaseResult | None] = [None] * len(selected)
        with ThreadPoolExecutor(
            max_workers=min(config.jobs, len(selected)),
            thread_name_prefix="parity-case",
        ) as pool:
            futures: dict[Future[CaseResult], int] = {
                pool.submit(configured_case, case): index for index, case in enumerate(selected)
            }
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
        cases = [result for result in ordered if result is not None]
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
    input_fixtures: Mapping[str, Any] | None = None,
    input_schemas: Mapping[str, FrameSchema] | None = None,
    relationships: Sequence[Relationship] = (),
    input_binding: Literal["keyword", "positional"] = "keyword",
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
    has_single = fixture is not None or schema is not None
    has_bundle = input_fixtures is not None or input_schemas is not None
    if has_single == has_bundle:
        raise ValueError(
            "verify requires exactly one fixture/schema or input_fixtures/input_schemas"
        )
    if has_single and (relationships or input_binding != "keyword"):
        raise ValueError(
            "relationships and input_binding apply only to input_fixtures/input_schemas"
        )

    # Fixture loaders and replay canonicalize chunk layout. Do the same before
    # live observation so chunk-sensitive Arrow callables see identical inputs.
    live_bundle: InputBundle | None = None
    bundle_schemas: dict[str, FrameSchema] | None = None
    if has_single:
        campaign_fixture: CampaignInput | None = (
            to_arrow(fixture).combine_chunks() if fixture is not None else None
        )
        selected_schema = schema or (
            infer_schema(campaign_fixture) if isinstance(campaign_fixture, pa.Table) else None
        )
        if selected_schema is None:  # pragma: no cover - guarded above
            raise ValueError("verify requires either fixture or schema")
    else:
        raw_fixtures = dict(input_fixtures or {})
        explicit_schemas = dict(input_schemas or {})
        if raw_fixtures and explicit_schemas and set(raw_fixtures) != set(explicit_schemas):
            raise ValueError("input_fixtures and input_schemas must use the same input names")
        names = list(raw_fixtures or explicit_schemas)
        if not 2 <= len(names) <= 3:
            raise ValueError("verify input bundles require two or three named inputs")
        converted = {name: to_arrow(raw_fixtures[name]).combine_chunks() for name in raw_fixtures}
        bundle_schemas = {
            name: explicit_schemas.get(name) or infer_schema(converted[name]) for name in names
        }
        live_bundle = InputBundle(
            inputs={name: InputSpec(input_schema=bundle_schemas[name]) for name in names},
            relationships=list(relationships),
            binding=input_binding,
        )
        validate_bundle_schemas(live_bundle, bundle_schemas)
        campaign_fixture = converted if len(converted) == len(names) else None
        selected_schema = None

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
        case_input: dict[str, Any] = (
            {"input_schema": selected_schema}
            if live_bundle is None
            else {"input_bundle": live_bundle}
        )
        artifact_case = CaseConfig(
            name="live",
            reference=reference_spec,
            candidate=candidate_spec,
            comparison=comparison,
            generation=generation,
            performance=performance,
            **case_input,
        )

    def bound_live_input(value: CampaignInput) -> ArrowInputBundle:
        if isinstance(value, pa.Table):
            return value
        if live_bundle is None or live_bundle.binding == "keyword":
            return value
        return tuple(value[name] for name in live_bundle.inputs)

    confirmation_pair_runner: PairRunner | None = None
    if reference_spec is not None and candidate_spec is not None:

        def run_clean_live_pair(value: CampaignInput) -> tuple[Observation, Observation]:
            with (
                IsolatedExecutionSession(reference_spec) as clean_reference,
                IsolatedExecutionSession(candidate_spec) as clean_candidate,
                ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="parity-live-confirm"
                ) as clean_pool,
            ):
                reference_future = clean_pool.submit(
                    clean_reference.execute,
                    bound_live_input(value),
                )
                candidate_future = clean_pool.submit(
                    clean_candidate.execute,
                    bound_live_input(value),
                )
                return reference_future.result(), candidate_future.result()

        confirmation_pair_runner = run_clean_live_pair

    def reference_runner(value: CampaignInput) -> Observation:
        return execute_callable_current(
            reference,
            bound_live_input(value),
            adapter=reference_adapter,
            pandas_input=reference_pandas_input,
            record_distributions=reference_distributions,
        )

    def candidate_runner(value: CampaignInput) -> Observation:
        return execute_callable_current(
            candidate,
            bound_live_input(value),
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
                "input_bundle": live_bundle,
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
        fixture=campaign_fixture,
        input_bundle=live_bundle,
        bundle_schemas=bundle_schemas,
        comparison=comparison,
        generation=generation,
        performance_config=performance,
        artifact_store=ArtifactStore(artifact_dir),
        reference_runner=reference_runner,
        candidate_runner=candidate_runner,
        artifact_case=artifact_case,
        reference_spec=reference_spec,
        candidate_spec=candidate_spec,
        confirmation_pair_runner=confirmation_pair_runner,
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


def _verify_manifest(root: Path) -> dict[str, Any]:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ReplayError("artifact directory is missing or invalid") from error
    manifest_path = resolved_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ReplayError("artifact manifest must be a regular file")
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplayError("artifact manifest is missing or invalid") from error
    if type(manifest.get("version")) is not int or manifest.get("version") != 2:
        raise ReplayError("unsupported artifact manifest")
    if not isinstance(manifest.get("files"), dict):
        raise ReplayError("unsupported artifact manifest")
    required = {"replay.json", "result.json"}
    missing = required - set(manifest["files"])
    if missing:
        raise ReplayError(
            f"artifact manifest is missing required file(s): {', '.join(sorted(missing))}"
        )
    for name, metadata in manifest["files"].items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(metadata, dict):
            raise ReplayError("artifact manifest contains an unsafe file entry")
        expected_bytes = metadata.get("bytes")
        expected_sha256 = metadata.get("sha256")
        if (
            type(expected_bytes) is not int
            or expected_bytes < 0
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            raise ReplayError(f"artifact manifest contains invalid metadata: {name}")
        file_path = resolved_root / name
        try:
            if file_path.is_symlink():
                raise ReplayError(f"artifact file is not a regular contained file: {name}")
            resolved_file = file_path.resolve(strict=True)
            if resolved_file.parent != resolved_root or not resolved_file.is_file():
                raise ReplayError(f"artifact file is not a regular contained file: {name}")
            actual_bytes = resolved_file.stat().st_size
        except OSError as error:
            raise ReplayError(f"artifact file is missing: {name}") from error
        if actual_bytes != expected_bytes:
            raise ReplayError(f"artifact size check failed: {name}")
        digest = hashlib.sha256()
        try:
            with resolved_file.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise ReplayError(f"artifact file could not be read: {name}") from error
        if digest.hexdigest() != expected_sha256:
            raise ReplayError(f"artifact integrity check failed: {name}")
    return manifest


def _replay_inputs(replay: dict[str, Any], manifest: dict[str, Any], root: Path) -> dict[str, Path]:
    raw_inputs = replay.get("inputs")
    if not isinstance(raw_inputs, list) or not 1 <= len(raw_inputs) <= 3:
        raise ReplayError("replay must contain one to three named inputs")
    inputs: dict[str, Path] = {}
    seen_files: set[str] = set()
    for raw in raw_inputs:
        if not isinstance(raw, dict) or set(raw) != {"name", "file"}:
            raise ReplayError("replay inputs contain an invalid entry")
        name, filename = raw["name"], raw["file"]
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or keyword.iskeyword(name)
            or name in inputs
            or not isinstance(filename, str)
            or Path(filename).name != filename
            or filename in seen_files
            or not filename.endswith(".arrow")
        ):
            raise ReplayError("replay inputs contain an unsafe entry")
        if filename not in manifest["files"]:
            raise ReplayError(f"artifact manifest does not bind replay input: {filename}")
        inputs[name] = root / filename
        seen_files.add(filename)
    return inputs


_REPLAY_BLOCKER_MESSAGES = {
    "live_callable": (
        "{side} live-callable target was not importable. Define it as a module-level import "
        "target in parity.toml, rerun parity check, and replay the new artifact."
    ),
    "external_python": (
        "{side}.python was outside the recorded configuration directory. Create or select "
        "a virtual environment inside the directory containing parity.toml, set {side}.python "
        "to its relative interpreter (or move parity.toml to a common containing directory), "
        "rerun parity check, and replay the new artifact from that configuration directory."
    ),
    "external_workdir": (
        "{side}.workdir was outside the recorded configuration directory. Move the target under "
        "the directory containing parity.toml or move parity.toml to a common containing "
        "directory, rerun parity check, and replay the new artifact from that directory."
    ),
    "external_command": (
        "{side}.command resolved outside the recorded configuration directory. Put the "
        "executable under the directory containing parity.toml, invoke it through PATH, or move "
        "parity.toml to a common containing directory; then rerun parity check and replay the new "
        "artifact from that configuration directory."
    ),
    "missing_command": (
        "{side}.command did not resolve to an existing project file. Fix or recreate the "
        "executable, rerun parity check, and replay the new artifact."
    ),
}

_ARTIFACT_REPLAY_BLOCKER_MESSAGES = {
    "external_artifact_root": (
        "the artifact directory was outside the recorded configuration directory. Move the "
        "artifact directory under the directory containing parity.toml, rerun parity check, and "
        "replay the new artifact."
    ),
}


def _reject_recorded_replay_blockers(replay: dict[str, Any]) -> None:
    raw = replay.get("replay_blockers")
    if raw is None:
        return
    if (
        not isinstance(raw, dict)
        or not raw
        or any(side not in {"artifact", "reference", "candidate"} for side in raw)
        or any(
            not isinstance(reason, str)
            or (
                reason not in _ARTIFACT_REPLAY_BLOCKER_MESSAGES
                if side == "artifact"
                else reason not in _REPLAY_BLOCKER_MESSAGES
            )
            for side, reason in raw.items()
        )
    ):
        raise ReplayError("artifact contains an invalid replay blocker declaration")
    details: list[str] = []
    if "artifact" in raw:
        details.append(_ARTIFACT_REPLAY_BLOCKER_MESSAGES[raw["artifact"]])
    details.extend(
        _REPLAY_BLOCKER_MESSAGES[raw[side]].format(side=side)
        for side in ("reference", "candidate")
        if side in raw
    )
    raise ReplayError("automatic replay is unavailable: " + " ".join(details))


def _missing_replay_target(side: str) -> ReplayError:
    return ReplayError(
        f"{side} live-callable target cannot be reconstructed for automatic replay. Define it as "
        "a module-level import target in parity.toml, rerun parity check, and replay the new "
        "artifact."
    )


def _restore_environment(
    case_data: dict[str, Any],
    *,
    sides: tuple[str, ...] = ("reference", "candidate"),
) -> None:
    for side in sides:
        spec = case_data.get(side)
        if not isinstance(spec, dict):
            raise _missing_replay_target(side)
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


def _replay_execution_root(replay: dict[str, Any], artifact_root: Path) -> Path:
    """Resolve the recorded project root without consulting process cwd."""

    path_base = replay.get("path_base")
    if not isinstance(path_base, dict) or set(path_base) != {"kind", "levels"}:
        raise ReplayError("replay path base is missing or invalid")
    levels = path_base.get("levels")
    if path_base.get("kind") != "artifact_ancestor" or type(levels) is not int:
        raise ReplayError("unsupported replay path base")
    if not 1 <= levels <= 64:
        raise ReplayError("replay path base ancestor count is invalid")

    base = artifact_root.resolve()
    for _ in range(levels):
        parent = base.parent
        if parent == base:
            raise ReplayError("replay path base escapes the artifact filesystem")
        base = parent
    if not base.is_dir():  # pragma: no cover - every resolved ancestor is a directory
        raise ReplayError("replay path base is missing or invalid")
    return base


def _resolve_replay_paths(
    case_data: dict[str, Any],
    execution_root: Path,
    *,
    sides: tuple[str, ...] = ("reference", "candidate"),
) -> None:
    """Resolve sanitized paths from the artifact-bound configuration directory."""

    base = execution_root.resolve()
    for side in sides:
        spec = case_data.get(side)
        if not isinstance(spec, dict):
            raise _missing_replay_target(side)
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
                raise ReplayError(
                    f"replay {side} {field} must be relative to the recorded configuration "
                    "directory; place it under the directory containing parity.toml and run "
                    "replay from there"
                )
            lexical = Path(os.path.abspath(base / relative))
            if not lexical.is_relative_to(base):
                raise ReplayError(
                    f"replay {side} {field} paths must stay inside the recorded configuration "
                    "directory; move it under the directory containing parity.toml and run "
                    "replay from there"
                )
            if field == "python":
                # A normal project venv ends in a symlink to the host's base
                # Python. The project-local launch path is authoritative; its
                # environment identity would be lost by dereferencing it. All
                # parent directories must remain canonically inside the project;
                # only the final executable symlink may target the host Python.
                if not lexical.parent.resolve().is_relative_to(base):
                    raise ReplayError(
                        f"replay {side}.python parent directories must stay inside the recorded "
                        "configuration directory; recreate the virtual environment under the "
                        "directory containing parity.toml"
                    )
                if not lexical.is_file():
                    raise ReplayError(
                        f"replay {side} python path must be an existing file; recreate the "
                        "project-local virtual environment or rerun parity check with its current "
                        "relative interpreter"
                    )
                spec[field] = lexical
                continue
            resolved = lexical.resolve()
            if not resolved.is_relative_to(base):
                raise ReplayError(
                    f"replay {side}.{field} must stay inside the recorded configuration "
                    "directory; move it under the directory containing parity.toml and run "
                    "replay from there"
                )
            spec[field] = resolved
        command = spec.get("command")
        if isinstance(command, list) and command:
            raw_executable = command[0]
            if not isinstance(raw_executable, str):
                raise ReplayError("invalid replay command declaration")
            path_like = (
                Path(raw_executable).is_absolute()
                or raw_executable.startswith(".")
                or os.sep in raw_executable
                or (os.altsep is not None and os.altsep in raw_executable)
            )
            if path_like:
                if Path(raw_executable).is_absolute():
                    raise ReplayError("replay command paths must be relative")
                launch_root = spec.get("workdir")
                if not isinstance(launch_root, Path):
                    launch_root = base
                executable = Path(os.path.abspath(launch_root / raw_executable))
                try:
                    resolved_executable = executable.resolve(strict=True)
                except OSError as error:
                    raise ReplayError(
                        "replay command paths must be existing files inside the recorded "
                        "configuration directory"
                    ) from error
                if (
                    not executable.is_relative_to(base)
                    or not resolved_executable.is_relative_to(base)
                    or not resolved_executable.is_file()
                ):
                    raise ReplayError(
                        "replay command paths must be existing files inside the recorded "
                        "configuration directory"
                    )
                command[0] = str(executable)


def replay_artifact(path: str | Path) -> SuiteResult:
    """Validate an artifact manifest and re-run its exact saved input."""

    started = time.perf_counter()
    root = _artifact_root(Path(path).resolve())
    manifest = _verify_manifest(root)
    try:
        replay: dict[str, Any] = json.loads((root / "replay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplayError("replay.json is missing or invalid") from error
    replay_version = replay.get("version")
    if (
        type(replay_version) is not int
        or replay_version != 2
        or not isinstance(replay.get("case"), dict)
    ):
        raise ReplayError("unsupported replay contract")
    replay_inputs = _replay_inputs(replay, manifest, root)
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
    _reject_recorded_replay_blockers(replay)
    execution_root = _replay_execution_root(replay, root)
    case_data = dict(replay["case"])
    invocation_arguments = (
        case_data.get("static_args"),
        case_data.get("static_kwargs"),
        case_data.get("reference_kwargs"),
        case_data.get("candidate_kwargs"),
    )
    if any(_contains_redaction(value) for value in invocation_arguments):
        raise ReplayError("redacted static arguments cannot be replayed automatically")
    if any(_contains_redaction(case_data.get(side)) for side in ("reference", "candidate")):
        raise ReplayError("redacted target configuration cannot be replayed automatically")
    _restore_environment(case_data)
    _resolve_replay_paths(case_data, execution_root)
    raw_bundle = case_data.get("input_bundle")
    if raw_bundle is None:
        if set(replay_inputs) != {"input"}:
            raise ReplayError("single-input case requires exactly one input named 'input'")
        case_data["fixture"] = replay_inputs["input"]
    else:
        if not isinstance(raw_bundle, dict):
            raise ReplayError("artifact input bundle does not match its case contract")
        raw_specs = raw_bundle.get("inputs")
        if (
            not isinstance(raw_bundle, dict)
            or not isinstance(raw_specs, dict)
            or set(raw_specs) != set(replay_inputs)
            or not 2 <= len(replay_inputs) <= 3
        ):
            raise ReplayError("artifact input bundle does not match its case contract")
        # JSON object ordering is not a trusted invocation contract (the artifact
        # writer sorts keys for deterministic files). Replay's separately
        # hash-bound inputs list is authoritative for positional binding order.
        ordered_specs = {name: raw_specs[name] for name in replay_inputs}
        raw_bundle["inputs"] = ordered_specs
        raw_specs = ordered_specs
        for name, path in replay_inputs.items():
            raw_spec = raw_specs.get(name)
            if not isinstance(raw_spec, dict):
                raise ReplayError("artifact contains an invalid bundled input contract")
            raw_spec["fixture"] = path
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
