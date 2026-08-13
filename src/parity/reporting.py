"""Data-safe JSON, Markdown, terminal, and JUnit reports."""

from __future__ import annotations

import json
import os
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from parity.execution import redact_text
from parity.models import CaseResult, ExampleResult, Status, SuiteResult

ReportFormat = Literal["json", "markdown", "github", "terminal", "junit"]


def _artifact_name(path: Path | None) -> str | None:
    if path is None:
        return None
    parts = path.parts[-3:]
    return "/".join(parts)


def _failure_payload(failure: ExampleResult) -> dict[str, Any]:
    kinds = Counter(mismatch.kind.value for mismatch in failure.mismatches)
    return {
        "source": redact_text(failure.source),
        "status": failure.status.value,
        "mismatch_counts": dict(sorted(kinds.items())),
        # Values and verbose mismatch details are intentionally artifact-only.
        "artifact": _artifact_name(failure.artifact),
        "reference_metrics": (
            failure.reference_metrics.model_dump(mode="json") if failure.reference_metrics else None
        ),
        "candidate_metrics": (
            failure.candidate_metrics.model_dump(mode="json") if failure.candidate_metrics else None
        ),
    }


def _case_payload(case: CaseResult) -> dict[str, Any]:
    return {
        "name": case.name,
        "status": case.status.value,
        "examples_run": case.examples_run,
        "deterministic_examples": case.deterministic_examples,
        "generated_examples": case.generated_examples,
        "failures": [_failure_payload(failure) for failure in case.failures],
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
        ],
        "performance": case.performance.model_dump(mode="json") if case.performance else None,
        "elapsed_seconds": case.elapsed_seconds,
    }


def report_payload(result: SuiteResult) -> dict[str, Any]:
    """Return the stable, data-eliding JSON report contract."""

    return {
        "schema_version": 1,
        "status": result.status.value,
        "cases": [_case_payload(case) for case in result.cases],
        "elapsed_seconds": result.elapsed_seconds,
        "parity_version": result.parity_version,
    }


def render_json(result: SuiteResult, *, pretty: bool = True) -> str:
    """Render a machine-readable report without compared cell values."""

    return json.dumps(
        report_payload(result),
        indent=2 if pretty else None,
        sort_keys=pretty,
        allow_nan=False,
        separators=None if pretty else (",", ":"),
    ) + ("\n" if pretty else "")


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}x"


def render_markdown(result: SuiteResult) -> str:
    """Render a GitHub-friendly suite summary."""

    passed = sum(case.status is Status.PASSED for case in result.cases)
    lines = [
        "# Parity verification",
        "",
        f"**{result.status.value.upper()}** — {passed}/{len(result.cases)} cases passed "
        f"in {result.elapsed_seconds:.3f}s.",
        "",
        "| Case | Status | Examples | Failures | Runtime ratio | Memory ratio |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
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
                    str(len(case.failures)),
                    _ratio(performance.speed_ratio if performance else None),
                    _ratio(performance.memory_ratio if performance else None),
                ]
            )
            + " |"
        )
    failed_cases = [case for case in result.cases if case.status is not Status.PASSED]
    if failed_cases:
        lines.extend(["", "## Findings", ""])
        for case in failed_cases:
            counts = Counter(
                mismatch.kind.value for failure in case.failures for mismatch in failure.mismatches
            )
            finding = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
            if not finding:
                finding = case.status.value
            artifacts = sorted(
                filter(None, (_artifact_name(failure.artifact) for failure in case.failures))
            )
            suffix = f"; artifact: `{artifacts[0]}`" if artifacts else ""
            lines.append(f"- **{case.name}**: {finding}{suffix}")
            for diagnosis in case.diagnoses:
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


def render_github_summary(result: SuiteResult) -> str:
    """Alias with an explicit name for ``GITHUB_STEP_SUMMARY`` output."""

    return render_markdown(result)


def render_terminal(result: SuiteResult, *, color: bool = False, console: Any | None = None) -> str:
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
        line = (
            f"  {status(case.status):<10} {case.name}  "
            f"{case.examples_run} example(s), {len(case.failures)} failure(s)"
        )
        if case.performance and case.performance.speed_ratio is not None:
            line += f", runtime {case.performance.speed_ratio:.2f}x"
        lines.append(line)
        kinds = Counter(
            mismatch.kind.value for failure in case.failures for mismatch in failure.mismatches
        )
        if kinds:
            lines.append(
                "             "
                + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
            )
        for diagnosis in case.diagnoses:
            lines.append(
                f"             diagnosis ({diagnosis.confidence}): {redact_text(diagnosis.title)}"
            )
        for artifact in filter(
            None, (_artifact_name(failure.artifact) for failure in case.failures)
        ):
            lines.append(f"             artifact: {artifact}")
    rendered = "\n".join(lines) + "\n"
    if console is not None:
        console.print(rendered, end="")
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
                {"message": summary or "execution error", "type": "ParityExecutionError"},
            )
        elif case.status is Status.SKIPPED:
            ET.SubElement(testcase, "skipped", {"message": "case skipped"})
    ET.indent(suite, space="  ")
    return ET.tostring(suite, encoding="unicode", xml_declaration=False) + "\n"


def write_report(result: SuiteResult, format: ReportFormat, destination: str | Path) -> Path:
    """Atomically write a report to ``destination``."""

    renderers = {
        "json": render_json,
        "markdown": render_markdown,
        "github": render_github_summary,
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


def write_json(result: SuiteResult, destination: str | Path) -> Path:
    """Compatibility convenience for CLI and CI integrations."""

    return write_report(result, "json", destination)


def write_junit(result: SuiteResult, destination: str | Path) -> Path:
    """Compatibility convenience for CLI and CI integrations."""

    return write_report(result, "junit", destination)


__all__ = [
    "render_github_summary",
    "render_json",
    "render_junit",
    "render_markdown",
    "render_terminal",
    "report_payload",
    "write_json",
    "write_junit",
    "write_report",
]
