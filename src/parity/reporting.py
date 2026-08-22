"""Data-safe JSON, Markdown, terminal, and JUnit reports."""

from __future__ import annotations

import builtins
import errno
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic_core.core_schema import ErrorType

from parity.exception_semantics import normalize_exception_details
from parity.execution import redact_text
from parity.models import CaseResult, ExampleResult, Mismatch, MismatchKind, Status, SuiteResult

ReportFormat = Literal["json", "markdown", "github", "terminal", "junit"]

_STABILITY_PATH = re.compile(r"^\$(reference|candidate)\.stability\[(\d+)\]$")
_PAIR_STABILITY_PATH = re.compile(r"^\$campaign\.stability\[(\d+)\]$")
_INDEXED_PATH = re.compile(r"\[(?:-?\d+|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\]")
_SAFE_PATH_ROOT = re.compile(
    r"^\$(?:(?:reference|candidate|campaign|performance|inputs|result)(?:\[\*\])?|\[\*\])?$"
)
_SAFE_PERFORMANCE_REASON = re.compile(r"^[A-Za-z0-9 .%();:+x-]{1,512}$")
_SAFE_PERFORMANCE_EVIDENCE_ERROR = re.compile(
    r"^(?:"
    r"enforced runtime gate requires finite positive reference timing evidence for every run "
    r"\(\d+/\d+ runs observed\)|"
    r"enforced runtime gate requires finite positive timing evidence for every paired run "
    r"\(\d+/\d+ pairs observed\)|"
    r"enforced runtime gate requires a finite positive runtime ratio and confidence interval|"
    r"benchmark requires finite non-negative timing metrics for every observation "
    r"\(\d+/\d+ observed\)|"
    r"enforced memory gate requires peak RSS evidence for every paired run "
    r"\(\d+/\d+ pairs observed\)|"
    r"enforced memory gate requires usable non-zero peak RSS evidence"
    r")$"
)
_BUILTIN_EXCEPTION_TYPES = frozenset(
    f"builtins.{name}"
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
)
_PYDANTIC_EXCEPTION_TYPES = frozenset(
    {
        "pydantic.ValidationError",
        "pydantic.error_wrappers.ValidationError",
        "pydantic_core.ValidationError",
        "pydantic_core._pydantic_core.ValidationError",
    }
)
_PUBLIC_EXCEPTION_TYPES = _BUILTIN_EXCEPTION_TYPES | _PYDANTIC_EXCEPTION_TYPES
_PYDANTIC_ERROR_CODES = frozenset(str(code) for code in get_args(ErrorType))
_LEGACY_PYDANTIC_ERROR_CODES = frozenset(
    {
        "type_error.dict",
        "type_error.float",
        "type_error.integer",
        "type_error.list",
        "type_error.none.not_allowed",
        "type_error.str",
        "value_error.any_str.max_length",
        "value_error.any_str.min_length",
        "value_error.extra",
        "value_error.missing",
        "value_error.number.not_ge",
        "value_error.number.not_gt",
        "value_error.number.not_le",
        "value_error.number.not_lt",
    }
)
_PUBLIC_API_TOKENS = frozenset(
    {
        "ndarray.ptp",
        "np.cast",
        "np.complex_",
        "np.float_",
        "np.product",
        "numpy.cast",
        "numpy.complex_",
        "numpy.float_",
        "numpy.product",
    }
)

_SAFE_MISMATCH_MESSAGES = frozenset(
    {
        "null values are not equivalent",
        "null differs from a value",
        "one result is tabular and the other is not",
        "one result is a series and the other is not",
        "mapping differs from non-mapping",
        "mapping keys differ",
        "sequence differs from non-sequence",
        "sequence lengths differ",
        "set differs from non-set",
        "set members differ",
        "datetime values differ",
        "duration values differ",
        "numeric values differ beyond tolerance",
        "boolean differs from a non-boolean value",
        "values differ",
        "one implementation raised and the other returned",
        "raised exceptions differ",
        "column dtype differs",
        "dtype differs",
        "series names differ",
        "series lengths differ",
        "column names are ambiguous under the selected name policy",
        "column sets differ",
        "column order differs",
        "row counts differ",
        "reference row has no equivalent candidate row",
        "candidate contains an unmatched row",
        "configured row key columns are unavailable",
        "row keys are not unique",
        "row key is not alignable under the comparison policy",
        "reference row key has no candidate row",
        "candidate row key has no reference row",
        "row key contains a non-scalar value",
        "input mutation behaviour differs",
    }
)
_MISMATCH_KIND_SUMMARIES = {
    MismatchKind.VALUE: "values differ",
    MismatchKind.SCHEMA: "schemas differ",
    MismatchKind.COLUMN: "columns differ",
    MismatchKind.DTYPE: "dtypes differ",
    MismatchKind.SHAPE: "shapes differ",
    MismatchKind.ROW: "rows differ",
    MismatchKind.EXCEPTION: "raised exception semantics differ",
    MismatchKind.MUTATION: "input mutation behaviour differs",
    MismatchKind.PERFORMANCE: "performance threshold was exceeded",
}


def _artifact_name(path: Path | None) -> str | None:
    if path is None:
        return None
    parts = path.parts[-3:]
    return "/".join(parts)


def _safe_mismatch_path(path: str | None) -> str:
    """Keep useful structural location without publishing user-defined names."""

    if not path:
        return "$"
    normalized = _INDEXED_PATH.sub("[*]", path)
    if normalized.startswith("$inputs/"):
        return "$inputs/<input>"
    root, separator, _field = normalized.partition(".")
    if _SAFE_PATH_ROOT.fullmatch(root) is None:
        return "$"
    return f"{root}.<field>" if separator else root


def _safe_mismatch_summary(failure: ExampleResult, mismatch: Mismatch) -> str:
    """Describe a mismatch without copying observed values or operational text."""

    if failure.status is Status.ERROR:
        path = mismatch.path or ""
        if path == "$performance":
            message = mismatch.message
            if message == "performance measurement runner is unavailable":
                return (
                    "performance runner is unavailable; rerun with --no-performance "
                    "or configure a supported runner"
                )
            if message == "performance measurement has no validated representative input":
                return (
                    "performance needs a validated input; add a fixture or generated schema, "
                    "or rerun with --no-performance"
                )
            benchmark_failure = re.fullmatch(
                r"(reference|candidate)( warmup)? benchmark did not return successfully \(.+\)",
                message,
            )
            if benchmark_failure is not None:
                side, warmup = benchmark_failure.groups()
                phase = " warmup" if warmup else ""
                return (
                    f"{side}{phase} benchmark execution failed; rerun with --no-performance "
                    "to verify semantics separately"
                )
            if _SAFE_PERFORMANCE_EVIDENCE_ERROR.fullmatch(message) is not None:
                return f"{message}; rerun with --no-performance to verify semantics separately"
            return "performance measurement could not be completed; rerun with --no-performance"
        if ".stability[" in path:
            return "nondeterminism prevented a reliable comparison"
        if path.endswith(".runtime"):
            return "runtime preflight could not be completed"
        if path.startswith("$reference"):
            return "reference execution could not be completed"
        if path.startswith("$candidate"):
            return "candidate execution could not be completed"
        return "Parity could not perform the comparison"
    if mismatch.kind is MismatchKind.EXCEPTION:
        semantic = _semantic_exception_summary(mismatch)
        if semantic is not None:
            return semantic
    if mismatch.message in _SAFE_MISMATCH_MESSAGES:
        return str(mismatch.message)
    return _MISMATCH_KIND_SUMMARIES[mismatch.kind]


def _public_exception_type(value: object) -> str:
    """Project only fixed exception identities; custom names can encode input data."""

    return value if isinstance(value, str) and value in _PUBLIC_EXCEPTION_TYPES else "custom"


def _public_exception_details(exception_type: object, value: object) -> dict[str, Any]:
    """Project finite, reviewed metadata sets into the data-eliding report."""

    details = normalize_exception_details(value)
    projected: dict[str, Any] = {}
    if locations := details.get("location_shapes"):
        projected["location_shapes"] = locations
    if isinstance(errno_value := details.get("errno"), int) and errno_value in errno.errorcode:
        projected["errno"] = errno_value
    if api_tokens := details.get("api_tokens"):
        safe_api_tokens = sorted(set(api_tokens) & _PUBLIC_API_TOKENS)
        if safe_api_tokens:
            projected["api_tokens"] = safe_api_tokens
    if exception_type in _PYDANTIC_EXCEPTION_TYPES and (codes := details.get("error_codes")):
        safe_codes = sorted(set(codes) & (_PYDANTIC_ERROR_CODES | _LEGACY_PYDANTIC_ERROR_CODES))
        if safe_codes:
            projected["error_codes"] = safe_codes
    if member_types := details.get("member_types"):
        safe_members = sorted(set(member_types) & _PUBLIC_EXCEPTION_TYPES)
        if safe_members:
            projected["member_types"] = safe_members
    return projected


def _safe_semantic_outcome(mismatch: Mismatch, side: str) -> dict[str, Any] | None:
    outcome = mismatch.details.get(f"{side}_outcome")
    if outcome not in {"return", "raise"}:
        return None
    payload: dict[str, Any] = {"outcome": outcome}
    if outcome == "return":
        return payload

    exception_type = mismatch.details.get(f"{side}_type")
    payload["exception_type"] = _public_exception_type(exception_type)
    details = _public_exception_details(
        exception_type,
        mismatch.details.get(f"{side}_exception_details"),
    )
    payload.update(details)
    return payload


def _semantic_outcome_text(side: str, outcome: dict[str, Any]) -> str:
    if outcome["outcome"] == "return":
        return f"{side} returned"
    rendered = f"{side} raised {outcome['exception_type']}"
    evidence: list[str] = []
    labels = {
        "api_tokens": "API",
        "error_codes": "codes",
        "location_shapes": "locations",
        "member_types": "members",
    }
    for key, label in labels.items():
        values = outcome.get(key)
        if isinstance(values, list) and values:
            evidence.append(f"{label}: {', '.join(values)}")
    if isinstance(outcome.get("errno"), int):
        evidence.append(f"errno: {outcome['errno']}")
    if evidence:
        rendered += " [" + "; ".join(evidence) + "]"
    return rendered


def _semantic_exception_summary(mismatch: Mismatch) -> str | None:
    reference = _safe_semantic_outcome(mismatch, "reference")
    candidate = _safe_semantic_outcome(mismatch, "candidate")
    if reference is None or candidate is None:
        return None
    return "; ".join(
        (
            _semantic_outcome_text("reference", reference),
            _semantic_outcome_text("candidate", candidate),
        )
    )


def _mismatch_summaries(failure: ExampleResult) -> list[dict[str, Any]]:
    """Return deterministic, data-safe mismatch evidence for one finding/error."""

    summaries: dict[str, dict[str, Any]] = {}
    for mismatch in failure.mismatches:
        payload: dict[str, Any] = {
            "kind": mismatch.kind.value,
            "summary": _safe_mismatch_summary(failure, mismatch),
            "path": _safe_mismatch_path(mismatch.path),
        }
        if failure.status is Status.FAILED and mismatch.kind is MismatchKind.EXCEPTION:
            for side in ("reference", "candidate"):
                if outcome := _safe_semantic_outcome(mismatch, side):
                    payload[side] = outcome
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        summaries[encoded] = payload
    return [summaries[key] for key in sorted(summaries)]


def _safe_performance_reason(reason: str) -> str:
    if (
        reason.startswith(
            ("candidate paired median runtime is ", "candidate paired median peak RSS is ")
        )
        and _SAFE_PERFORMANCE_REASON.fullmatch(reason) is not None
    ):
        return reason
    return "configured performance threshold was exceeded"


def _performance_payload(case: CaseResult) -> dict[str, Any] | None:
    if case.performance is None:
        return None
    payload = case.performance.model_dump(mode="json")
    payload["reasons"] = [_safe_performance_reason(item) for item in case.performance.reasons]
    return payload


def _failure_payload(failure: ExampleResult) -> dict[str, Any]:
    kinds = Counter(mismatch.kind.value for mismatch in failure.mismatches)
    return {
        "source": redact_text(failure.source),
        "status": failure.status.value,
        "finding_signature": failure.finding_signature,
        "approved": failure.approved,
        "mismatch_counts": dict(sorted(kinds.items())),
        "mismatches": _mismatch_summaries(failure),
        # Values and verbose mismatch details are intentionally artifact-only.
        "artifact": _artifact_name(failure.artifact),
        "reference_metrics": (
            failure.reference_metrics.model_dump(mode="json") if failure.reference_metrics else None
        ),
        "candidate_metrics": (
            failure.candidate_metrics.model_dump(mode="json") if failure.candidate_metrics else None
        ),
    }


def _ordered_failures(case: CaseResult) -> list[ExampleResult]:
    """Return deterministic finding order independent of discovery order."""

    return sorted(
        case.failures,
        key=lambda failure: (
            failure.finding_signature is None,
            failure.finding_signature or "",
            failure.status.value,
            failure.source,
        ),
    )


def _finding_count(case: CaseResult) -> int:
    """Derive the count from evidence so reports cannot contradict failures."""

    return len(
        {
            failure.finding_signature
            for failure in case.failures
            if failure.finding_signature is not None
        }
    )


def _stability_summaries(case: CaseResult) -> list[str]:
    """Render only structure encoded by Parity, never observed output text."""

    details: set[tuple[int, int, str]] = set()
    side_order = {"reference": 0, "candidate": 1}
    for failure in _ordered_failures(case):
        if not failure.source.startswith("deterministic:stability:"):
            continue
        for mismatch in failure.mismatches:
            path = mismatch.path or ""
            if matched := _STABILITY_PATH.fullmatch(path):
                side, raw_repeat = matched.groups()
                repeat = int(raw_repeat)
                details.add(
                    (
                        repeat,
                        side_order[side],
                        f"{side} changed on stability repeat {repeat}",
                    )
                )
            elif matched := _PAIR_STABILITY_PATH.fullmatch(path):
                repeat = int(matched.group(1))
                details.add(
                    (
                        repeat,
                        2,
                        f"reference/candidate pair changed on stability repeat {repeat}",
                    )
                )
    return [detail for _, _, detail in sorted(details)]


def _case_payload(case: CaseResult) -> dict[str, Any]:
    return {
        "name": case.name,
        "status": case.status.value,
        "examples_run": case.examples_run,
        "deterministic_examples": case.deterministic_examples,
        "generated_examples": case.generated_examples,
        "findings_discovered": _finding_count(case),
        "finding_limit_reached": case.finding_limit_reached,
        "failures": [_failure_payload(failure) for failure in _ordered_failures(case)],
        "diagnoses": [
            {
                "code": diagnosis.code,
                "title": redact_text(diagnosis.title),
                "explanation": redact_text(diagnosis.explanation),
                "confidence": diagnosis.confidence,
                "evidence": [redact_text(item) for item in diagnosis.evidence],
                "documentation_url": diagnosis.documentation_url,
            }
            for diagnosis in case.diagnoses
            if case.status is Status.FAILED
        ],
        "performance": _performance_payload(case),
        "provenance": case.provenance.model_dump(mode="json") if case.provenance else None,
        "compatibility": (
            case.compatibility.model_dump(mode="json") if case.compatibility else None
        ),
        "elapsed_seconds": case.elapsed_seconds,
    }


def report_payload(result: SuiteResult) -> dict[str, Any]:
    """Return the stable, data-eliding JSON report contract."""

    payload = {
        "schema_version": 4,
        "status": result.status.value,
        "cases": [_case_payload(case) for case in result.cases],
        "elapsed_seconds": result.elapsed_seconds,
        "parity_version": result.parity_version,
        "provenance": result.provenance.model_dump(mode="json") if result.provenance else None,
    }
    from parity.json_contracts import SuiteReportContract

    return SuiteReportContract.model_validate(payload).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )


def _provenance_warning(case: CaseResult) -> str | None:
    if case.provenance is None:
        return None
    if case.provenance.verification == "drifted":
        return "runtime provenance drifted; callable execution was blocked"
    return None


def render_json(result: SuiteResult, *, pretty: bool = True) -> str:
    """Render a machine-readable report without compared cell values."""

    return json.dumps(
        report_payload(result),
        indent=2 if pretty else None,
        sort_keys=pretty,
        allow_nan=False,
        separators=None if pretty else (",", ":"),
    ) + ("\n" if pretty else "")


def _confidence_label(confidence_level: float) -> str:
    percentage = f"{confidence_level * 100:.2f}".rstrip("0").rstrip(".")
    return f"{percentage}%"


def _ratio(
    value: float | None,
    interval: tuple[float, float] | None = None,
    confidence_level: float | None = None,
) -> str:
    if value is None:
        return "—"
    rendered = f"{value:.2f}x"
    if interval is not None:
        confidence = (
            f"{_confidence_label(confidence_level)} " if confidence_level is not None else ""
        )
        rendered += f" ({confidence}CI {interval[0]:.2f}-{interval[1]:.2f}x)"
    return rendered


def render_markdown(result: SuiteResult) -> str:
    """Render a GitHub-friendly suite summary."""

    passed = sum(case.status is Status.PASSED for case in result.cases)
    lines = [
        "# Parity verification",
        "",
        f"**{result.status.value.upper()}** — {passed}/{len(result.cases)} cases passed "
        f"in {result.elapsed_seconds:.3f}s.",
        "",
        "| Case | Status | Examples | Findings | Approved | Runtime ratio | Memory ratio |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in result.cases:
        performance = case.performance
        lines.append(
            "| "
            + " | ".join(
                [
                    case.name.replace("|", "\\|"),
                    case.status.value,
                    str(case.examples_run),
                    str(_finding_count(case)),
                    str(sum(failure.approved for failure in case.failures)),
                    _ratio(
                        performance.speed_ratio if performance else None,
                        performance.speed_ratio_ci if performance else None,
                        performance.confidence_level if performance else None,
                    ),
                    _ratio(
                        performance.memory_ratio if performance else None,
                        performance.memory_ratio_ci if performance else None,
                        performance.confidence_level if performance else None,
                    ),
                ]
            )
            + " |"
        )
    warnings = [
        (case.name, warning)
        for case in result.cases
        if (warning := _provenance_warning(case)) is not None
    ]
    if warnings:
        lines.extend(["", "## Provenance warnings", ""])
        for name, warning in warnings:
            lines.append(f"- **{name}**: {warning}")
    limited_cases = [case.name for case in result.cases if case.finding_limit_reached]
    if limited_cases:
        lines.extend(["", "## Search limits", ""])
        for name in limited_cases:
            lines.append(
                f"- **{name}**: the finding limit was reached. Rerun with "
                "`--max-findings <higher-number>` to search for additional incompatibilities."
            )
    performance_cases = [case for case in result.cases if case.performance is not None]
    if performance_cases:
        lines.extend(["", "## Performance evidence", ""])
        for case in performance_cases:
            assert case.performance is not None
            performance = case.performance
            lines.append(
                f"- **{case.name}**: runtime "
                f"{_ratio(performance.speed_ratio, performance.speed_ratio_ci, performance.confidence_level)}; "
                "memory "
                f"{_ratio(performance.memory_ratio, performance.memory_ratio_ci, performance.confidence_level)}"
            )
            for reason in performance.reasons:
                lines.append(f"  - gate reason: {_safe_performance_reason(reason)}")
    budget_cases = [case for case in result.cases if case.compatibility is not None]
    if budget_cases:
        lines.extend(["", "## Compatibility budget", ""])
        for case in budget_cases:
            assert case.compatibility is not None
            compatibility = case.compatibility
            lines.append(
                f"- **{case.name}**: {len(compatibility.approved_findings)} approved, "
                f"{len(compatibility.unapproved_findings)} unapproved, "
                f"{len(compatibility.unused_approvals)} no longer observed"
            )
    finding_cases = [case for case in result.cases if case.failures]
    if finding_cases:
        lines.extend(["", "## Findings", ""])
        for case in finding_cases:
            counts = Counter(
                mismatch.kind.value
                for failure in _ordered_failures(case)
                for mismatch in failure.mismatches
            )
            finding = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
            if not finding:
                finding = case.status.value
            artifacts = sorted(
                {
                    artifact
                    for failure in case.failures
                    if (artifact := _artifact_name(failure.artifact)) is not None
                }
            )
            signatures = _finding_count(case)
            signature_label = (
                f"; {signatures} distinct mismatch signature" + ("" if signatures == 1 else "s")
                if signatures
                else ""
            )
            lines.append(f"- **{case.name}**: {finding}{signature_label}")
            for failure in _ordered_failures(case):
                label = (
                    ("approved " if failure.approved else "")
                    + f"finding `{failure.finding_signature}`"
                    if failure.finding_signature is not None
                    else "execution error"
                )
                for mismatch in _mismatch_summaries(failure):
                    lines.append(f"  - {label}: {mismatch['summary']} at `{mismatch['path']}`")
            for artifact in artifacts:
                lines.append(f"  - artifact: `{artifact}`")
            for stability in _stability_summaries(case):
                lines.append(f"  - stability error: {stability}")
            for diagnosis in case.diagnoses if case.status is Status.FAILED else []:
                lines.append(
                    f"  - {redact_text(diagnosis.title)} ({diagnosis.confidence}): "
                    f"{redact_text(diagnosis.explanation)}"
                )
    lines.extend(
        [
            "",
            "Compared row values are omitted from this summary. Reproduce failures from their artifacts.",
            "",
        ]
    )
    return "\n".join(lines)


def render_terminal(
    result: SuiteResult,
    *,
    color: bool = False,
    console: Any | None = None,
    artifact_renderer: Callable[[Path], str] | None = None,
) -> str:
    """Render a concise terminal report, optionally with ANSI status colours."""

    colors = {
        Status.PASSED: "\x1b[32m",
        Status.FAILED: "\x1b[31m",
        Status.ERROR: "\x1b[31m",
        Status.SKIPPED: "\x1b[33m",
    }
    reset = "\x1b[0m"

    def status(value: Status) -> str:
        label = value.value.upper()
        return f"{colors[value]}{label}{reset}" if color else label

    lines = [
        f"Parity {status(result.status)}  {len(result.cases)} case(s)  "
        f"{result.elapsed_seconds:.3f}s"
    ]
    for case in result.cases:
        finding_count = _finding_count(case)
        operational_errors = sum(failure.status is Status.ERROR for failure in case.failures)
        line = f"  {status(case.status):<10} {case.name}  {case.examples_run} example(s), "
        line += f"{finding_count} finding(s)"
        approved_count = sum(failure.approved for failure in case.failures)
        if approved_count:
            line += f", {approved_count} approved"
        if operational_errors:
            line += f", {operational_errors} execution error(s)"
        if case.performance and case.performance.speed_ratio is not None:
            line += ", runtime " + _ratio(
                case.performance.speed_ratio,
                case.performance.speed_ratio_ci,
                case.performance.confidence_level,
            )
        if case.performance and case.performance.memory_ratio is not None:
            line += ", memory " + _ratio(
                case.performance.memory_ratio,
                case.performance.memory_ratio_ci,
                case.performance.confidence_level,
            )
        lines.append(line)
        if case.finding_limit_reached:
            lines.append(
                "             warning: finding limit reached; rerun with "
                "--max-findings <higher-number> to search for more"
            )
        if warning := _provenance_warning(case):
            lines.append(f"             warning: {warning}")
        if case.compatibility and case.compatibility.unused_approvals:
            lines.append(
                "             note: "
                f"{len(case.compatibility.unused_approvals)} approved finding(s) "
                "are no longer observed"
            )
        kinds = Counter(
            mismatch.kind.value
            for failure in _ordered_failures(case)
            for mismatch in failure.mismatches
        )
        if kinds:
            lines.append(
                "             "
                + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
            )
        for failure in _ordered_failures(case):
            label = (
                ("approved " if failure.approved else "") + f"finding {failure.finding_signature}"
                if failure.finding_signature is not None
                else "execution error"
            )
            for mismatch in _mismatch_summaries(failure):
                lines.append(f"             {label}: {mismatch['summary']} at {mismatch['path']}")
        for stability in _stability_summaries(case):
            lines.append(f"             stability error: {stability}")
        for diagnosis in case.diagnoses if case.status is Status.FAILED else []:
            lines.append(
                f"             diagnosis ({diagnosis.confidence}): {redact_text(diagnosis.title)}"
            )
        if case.performance is not None:
            for reason in case.performance.reasons:
                lines.append(f"             performance gate: {_safe_performance_reason(reason)}")
        artifacts = (
            (
                artifact_renderer(failure.artifact)
                if artifact_renderer is not None and failure.artifact is not None
                else _artifact_name(failure.artifact)
            )
            for failure in _ordered_failures(case)
        )
        for artifact in filter(None, artifacts):
            lines.append(f"             artifact: {artifact}")
    rendered = "\n".join(lines) + "\n"
    if console is not None:
        # Exception evidence deliberately uses square brackets. Rich must not
        # treat codes such as ``[int_from_float]`` as presentation markup.
        console.print(rendered, end="", markup=False)
    return rendered


def render_junit(result: SuiteResult) -> str:
    """Render one JUnit testcase per configured Parity case."""

    failures = sum(case.status is Status.FAILED for case in result.cases)
    errors = sum(case.status is Status.ERROR for case in result.cases)
    skipped = sum(case.status is Status.SKIPPED for case in result.cases)
    suite = ET.Element(
        "testsuite",
        {
            "name": "parity",
            "tests": str(len(result.cases)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{result.elapsed_seconds:.9f}",
        },
    )
    for case in result.cases:
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "parity",
                "name": case.name,
                "time": f"{case.elapsed_seconds:.9f}",
            },
        )
        kinds = Counter(
            mismatch.kind.value for failure in case.failures for mismatch in failure.mismatches
        )
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        stability = "; ".join(_stability_summaries(case))
        if case.status is Status.FAILED:
            ET.SubElement(
                testcase,
                "failure",
                {"message": summary or "semantic mismatch", "type": "ParityMismatch"},
            )
        elif case.status is Status.ERROR:
            ET.SubElement(
                testcase,
                "error",
                {
                    "message": (
                        f"stability error: {stability}"
                        if stability
                        else summary or "execution error"
                    ),
                    "type": "ParityExecutionError",
                },
            )
        elif case.status is Status.SKIPPED:
            ET.SubElement(testcase, "skipped", {"message": "case skipped"})
        approved = [
            failure.finding_signature
            for failure in _ordered_failures(case)
            if failure.approved and failure.finding_signature is not None
        ]
        if approved:
            ET.SubElement(testcase, "system-out").text = (
                "approved compatibility findings: " + ", ".join(approved)
            )
    ET.indent(suite, space="  ")
    return ET.tostring(suite, encoding="unicode", xml_declaration=False) + "\n"


def write_report(result: SuiteResult, format: ReportFormat, destination: str | Path) -> Path:
    """Atomically write a report to ``destination``."""

    renderers = {
        "json": render_json,
        "markdown": render_markdown,
        "github": render_markdown,
        "terminal": render_terminal,
        "junit": render_junit,
    }
    try:
        content = renderers[format](result)
    except KeyError as error:
        raise ValueError(f"unsupported report format: {format}") from error
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "render_json",
    "render_junit",
    "render_markdown",
    "render_terminal",
    "report_payload",
    "write_report",
]
