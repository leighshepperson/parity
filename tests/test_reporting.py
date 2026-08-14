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
    render_json,
    render_junit,
    render_markdown,
    render_terminal,
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
        finding_signature="ms1:" + "b" * 64,
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
    assert payload["schema_version"] == 3
    assert payload["status"] == "failed"
    assert payload["cases"][0]["findings_discovered"] == 1
    assert payload["cases"][0]["failures"][0]["finding_signature"] == "ms1:" + "b" * 64
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


def test_human_reports_show_counts_not_compared_data(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    markdown = render_markdown(suite)
    assert "# Parity verification" in markdown
    assert "1 value" in markdown
    assert "1 distinct mismatch signature" in markdown
    assert (
        write_report(suite, "github", tmp_path / "summary.md").read_text(encoding="utf-8")
        == markdown
    )
    terminal = render_terminal(suite)
    assert "Parity FAILED" in terminal
    assert "1 value" in terminal
    assert "1 distinct mismatch signature(s)" in terminal
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


def test_report_writer_is_atomic_for_every_file_format(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    json_path = write_report(suite, "json", tmp_path / "reports" / "result.json")
    junit_path = write_report(suite, "junit", tmp_path / "reports" / "junit.xml")
    markdown_path = write_report(suite, "markdown", tmp_path / "reports" / "summary.md")
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert ET.parse(junit_path).getroot().tag == "testsuite"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Parity")


def test_json_orders_signed_findings_by_signature_then_unsigned_errors(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    first = suite.cases[0].failures[0]
    first.finding_signature = "ms1:" + "f" * 64
    second = first.model_copy(
        update={
            "source": "second",
            "finding_signature": "ms1:" + "a" * 64,
            "artifact": None,
        }
    )
    operational = first.model_copy(
        update={
            "source": "operational",
            "status": Status.ERROR,
            "finding_signature": None,
            "artifact": None,
        }
    )
    suite.cases[0] = suite.cases[0].model_copy(
        update={
            "failures": [operational, first, second],
        }
    )

    case_payload = json.loads(render_json(suite))["cases"][0]
    failures = case_payload["failures"]

    assert case_payload["findings_discovered"] == 2
    assert [failure["finding_signature"] for failure in failures] == [
        "ms1:" + "a" * 64,
        "ms1:" + "f" * 64,
        None,
    ]


def test_markdown_lists_every_distinct_artifact_deterministically(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    first = suite.cases[0].failures[0]
    first.artifact = tmp_path / "artifact-root" / "orders" / "z-campaign"
    second = first.model_copy(
        update={
            "finding_signature": "ms1:" + "a" * 64,
            "artifact": tmp_path / "artifact-root" / "orders" / "a-campaign",
        }
    )
    duplicate = second.model_copy()
    suite.cases[0].failures = [first, second, duplicate]

    rendered = render_markdown(suite)

    artifact_lines = [line for line in rendered.splitlines() if "artifact:" in line]
    assert artifact_lines == [
        "  - artifact: `artifact-root/orders/a-campaign`",
        "  - artifact: `artifact-root/orders/z-campaign`",
    ]
    assert str(tmp_path) not in rendered


def test_case_result_json_roundtrip_preserves_derived_finding_count(tmp_path: Path) -> None:
    original = _suite(tmp_path).cases[0]

    restored = CaseResult.model_validate_json(original.model_dump_json())

    assert restored.findings_discovered == 1
    assert restored == original


def test_case_result_always_derives_count_from_current_failures(tmp_path: Path) -> None:
    failure = _suite(tmp_path).cases[0].failures[0]
    case = CaseResult.model_validate(
        {
            "name": "derived",
            "status": Status.FAILED,
            "findings_discovered": 99,
            "failures": [failure],
        }
    )
    assert case.findings_discovered == 1

    second = failure.model_copy(update={"finding_signature": "ms1:" + "c" * 64})
    case.failures = [failure, second]
    assert case.findings_discovered == 2

    case.failures.pop()
    assert case.findings_discovered == 1


def test_stability_errors_report_side_and_repeat_without_observed_values(
    tmp_path: Path,
) -> None:
    suite = _suite(tmp_path)
    stability = ExampleResult(
        source="deterministic:stability:reference,candidate:repeat-2",
        status=Status.ERROR,
        mismatches=[
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="candidate secret@example.test changed on stability repeat 2",
                path="$candidate.stability[2]",
            ),
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="reference secret@example.test changed on stability repeat 2",
                path="$reference.stability[2]",
            ),
        ],
    )
    suite.status = Status.ERROR
    suite.cases[0] = CaseResult(
        name="orders",
        status=Status.ERROR,
        examples_run=1,
        deterministic_examples=1,
        failures=[stability],
    )

    json_payload = json.loads(render_json(suite))
    markdown = render_markdown(suite)
    terminal = render_terminal(suite)
    junit = ET.fromstring(render_junit(suite))

    assert json_payload["cases"][0]["failures"][0]["source"] == stability.source
    assert "reference changed on stability repeat 2" in markdown
    assert "candidate changed on stability repeat 2" in markdown
    assert markdown.index("reference changed") < markdown.index("candidate changed")
    assert "reference changed on stability repeat 2" in terminal
    assert "candidate changed on stability repeat 2" in terminal
    junit_error = junit.find("testcase/error")
    assert junit_error is not None
    assert junit_error.attrib["message"] == (
        "stability error: reference changed on stability repeat 2; "
        "candidate changed on stability repeat 2"
    )
    assert "secret@example.test" not in render_json(suite) + markdown + terminal + render_junit(
        suite
    )
