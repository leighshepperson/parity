from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from parity.models import (
    CaseProvenance,
    CaseResult,
    Diagnosis,
    ExampleResult,
    Mismatch,
    MismatchKind,
    Status,
    SuiteProvenance,
    SuiteResult,
)
from parity.provenance import DistributionProvenance, RuntimeProvenance
from parity.reporting import (
    render_github_summary,
    render_json,
    render_junit,
    render_markdown,
    render_terminal,
    write_json,
    write_junit,
    write_report,
)


def _suite(tmp_path: Path) -> SuiteResult:
    runtime = RuntimeProvenance(
        python_implementation="CPython",
        python_version="3.12.7",
        platform_system="Linux",
        platform_machine="x86_64",
        parity_version="0.1.0",
        distributions=(
            DistributionProvenance(name="skrub", status="installed", version="0.11.dev0"),
        ),
    )
    failure = ExampleResult(
        source="fixture /private/customer/orders.parquet",
        status=Status.FAILED,
        artifact=tmp_path / "orders" / "campaign" / "result.json",
        mismatches=[
            Mismatch(
                kind=MismatchKind.VALUE,
                message="secret@example.test changed to other@example.test",
                path="row[0].email",
                reference="secret@example.test",
                candidate="other@example.test",
            )
        ],
    )
    return SuiteResult(
        status=Status.FAILED,
        cases=[
            CaseResult(
                name="orders",
                status=Status.FAILED,
                examples_run=10,
                generated_examples=10,
                failures=[failure],
                diagnoses=[
                    Diagnosis(
                        code="missing-values",
                        title="Missing-value semantics differ",
                        explanation="Check joins and grouping keys.",
                        confidence="high",
                        evidence=["A minimized null differs."],
                    )
                ],
                provenance=CaseProvenance(reference=runtime, candidate=runtime),
                elapsed_seconds=0.25,
            ),
            CaseResult(name="customers", status=Status.PASSED, examples_run=5, elapsed_seconds=0.1),
        ],
        elapsed_seconds=0.35,
        provenance=SuiteProvenance(orchestrator=runtime, config_sha256="a" * 64),
    )


def test_json_report_is_machine_readable_and_elides_values(tmp_path: Path) -> None:
    rendered = render_json(_suite(tmp_path))
    payload = json.loads(rendered)
    assert payload["schema_version"] == 2
    assert payload["status"] == "failed"
    assert payload["cases"][0]["failures"][0]["mismatch_counts"] == {"value": 1}
    assert payload["provenance"]["config_sha256"] == "a" * 64
    assert payload["cases"][0]["provenance"]["reference"]["distributions"][0] == {
        "name": "skrub",
        "status": "installed",
        "version": "0.11.dev0",
    }
    assert "secret@example.test" not in rendered
    assert "/private/customer" not in rendered
    assert str(tmp_path) not in rendered


def test_legacy_replay_provenance_is_visibly_unverified(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    assert suite.cases[0].provenance is not None
    suite.cases[0].provenance.verification = "unverified"

    markdown = render_markdown(suite)
    terminal = render_terminal(suite)

    assert "not exact" in markdown
    assert "not exact" in terminal


def test_human_reports_show_counts_not_compared_data(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    markdown = render_markdown(suite)
    assert "# Parity verification" in markdown
    assert "1 value" in markdown
    assert render_github_summary(suite) == markdown
    terminal = render_terminal(suite)
    assert "Parity FAILED" in terminal
    assert "1 value" in terminal
    assert "secret@example.test" not in markdown + terminal


def test_terminal_can_emit_to_console_like_object(tmp_path: Path) -> None:
    class Console:
        rendered = ""

        def print(self, value: str, *, end: str = "\n") -> None:
            self.rendered += value + end

    console = Console()
    returned = render_terminal(_suite(tmp_path), console=console)
    assert console.rendered == returned


def test_junit_is_valid_and_data_safe(tmp_path: Path) -> None:
    rendered = render_junit(_suite(tmp_path))
    root = ET.fromstring(rendered)
    assert root.attrib == {
        "name": "parity",
        "tests": "2",
        "failures": "1",
        "errors": "0",
        "skipped": "0",
        "time": "0.350000000",
    }
    assert root.find("testcase/failure") is not None
    assert "secret@example.test" not in rendered


def test_report_writers_are_compatible_and_atomic(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    json_path = write_json(suite, tmp_path / "reports" / "result.json")
    junit_path = write_junit(suite, tmp_path / "reports" / "junit.xml")
    markdown_path = write_report(suite, "markdown", tmp_path / "reports" / "summary.md")
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert ET.parse(junit_path).getroot().tag == "testsuite"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Parity")
