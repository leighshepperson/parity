from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from parity.canonical import ExceptionInfo, Raise, Return
from parity.comparison import compare
from parity.models import (
    CaseProvenance,
    CaseResult,
    Diagnosis,
    ExampleResult,
    Mismatch,
    MismatchKind,
    PerformanceResult,
    RunMetrics,
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
        finding_signature="ms3:" + "b" * 64,
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
    assert payload["schema_version"] == 4
    assert payload["cases"][0]["failures"][0]["approved"] is False
    assert payload["status"] == "failed"
    assert payload["cases"][0]["findings_discovered"] == 1
    assert payload["cases"][0]["failures"][0]["finding_signature"] == "ms3:" + "b" * 64
    assert payload["cases"][0]["failures"][0]["mismatch_counts"] == {"value": 1}
    assert payload["cases"][0]["failures"][0]["mismatches"] == [
        {"kind": "value", "summary": "values differ", "path": "$"}
    ]
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
    assert "1 finding(s)" in terminal
    assert "secret@example.test" not in markdown + terminal


def test_exception_reports_explain_distinct_safe_semantics(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    validation = ExampleResult(
        source="generated",
        status=Status.FAILED,
        finding_signature="ms3:" + "1" * 64,
        mismatches=[
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="one implementation raised and the other returned",
                path="$result",
                details={
                    "reference_outcome": "return",
                    "candidate_outcome": "raise",
                    "candidate_type": "pydantic_core.ValidationError",
                    "candidate_exception_details": {
                        "error_codes": ["int_from_float"],
                        "location_shapes": ["field"],
                        "private": "secret@example.test",
                    },
                },
            )
        ],
    )
    removed_api = ExampleResult(
        source="generated",
        status=Status.FAILED,
        finding_signature="ms3:" + "2" * 64,
        mismatches=[
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="one implementation raised and the other returned",
                path="$result",
                details={
                    "reference_outcome": "return",
                    "candidate_outcome": "raise",
                    "candidate_type": "builtins.AttributeError",
                    "candidate_exception_details": {"api_tokens": ["np.cast"]},
                },
            )
        ],
    )
    suite.cases[0].failures = [validation, removed_api]

    payload = json.loads(render_json(suite))
    markdown = render_markdown(suite)
    terminal = render_terminal(suite)
    combined = render_json(suite) + markdown + terminal

    mismatches = [failure["mismatches"][0] for failure in payload["cases"][0]["failures"]]
    assert mismatches[0]["candidate"] == {
        "outcome": "raise",
        "exception_type": "pydantic_core.ValidationError",
        "error_codes": ["int_from_float"],
        "location_shapes": ["field"],
    }
    assert mismatches[1]["candidate"] == {
        "outcome": "raise",
        "exception_type": "builtins.AttributeError",
        "api_tokens": ["np.cast"],
    }
    assert "candidate raised pydantic_core.ValidationError" in combined
    assert "codes: int_from_float" in combined
    assert "candidate raised builtins.AttributeError [API: np.cast]" in combined
    assert "finding ms3:" + "1" * 64 in terminal
    assert "finding ms3:" + "2" * 64 in terminal
    assert "secret@example.test" not in combined


def test_exception_reports_redact_identifier_shaped_dynamic_metadata(tmp_path: Path) -> None:
    private = "patient_hiv_positive_4938221"
    dynamic_type = type(private, (Exception,), {"__module__": "clinical"})
    mismatch = compare(
        Return(None),
        Raise(ExceptionInfo.from_exception(dynamic_type(f"np.{private}"))),
    )[0]
    mismatch.details["candidate_exception_details"] = {
        "api_tokens": [f"np.{private}"],
        "error_codes": [private],
        "member_types": [f"clinical.{private}"],
        "errno": 4_938_221,
    }
    suite = _suite(tmp_path)
    suite.cases[0].failures = [
        ExampleResult(
            source="generated",
            status=Status.FAILED,
            finding_signature="ms3:" + "3" * 64,
            mismatches=[mismatch],
        )
    ]

    json_report = render_json(suite)
    markdown = render_markdown(suite)
    terminal = render_terminal(suite)
    payload = json.loads(json_report)
    candidate = payload["cases"][0]["failures"][0]["mismatches"][0]["candidate"]

    assert candidate == {"outcome": "raise", "exception_type": "custom"}
    assert "candidate raised custom" in json_report + markdown + terminal
    assert private not in json_report + markdown + terminal


def test_human_reports_show_confidence_intervals_and_gate_reasons(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    performance = PerformanceResult(
        reference=RunMetrics(duration_seconds=0.1, peak_rss_bytes=100),
        candidate=RunMetrics(duration_seconds=0.2, peak_rss_bytes=180),
        speed_ratio=2.0,
        speed_ratio_ci=(1.8, 2.2),
        memory_ratio=1.8,
        memory_ratio_ci=(1.6, 2.0),
        confidence_level=0.9,
        regression=True,
        reasons=[
            "candidate paired median runtime is 2.000x reference "
            "(90% CI 1.800-2.200x; limit 1.500x)"
        ],
    )
    suite.cases[0].performance = performance

    markdown = render_markdown(suite)
    terminal = render_terminal(suite)
    payload = json.loads(render_json(suite))

    assert "2.00x (90% CI 1.80-2.20x)" in markdown
    assert "1.80x (90% CI 1.60-2.00x)" in markdown
    assert "gate reason: candidate paired median runtime is 2.000x" in markdown
    assert "runtime 2.00x (90% CI 1.80-2.20x)" in terminal
    assert "memory 1.80x (90% CI 1.60-2.00x)" in terminal
    assert "performance gate: candidate paired median runtime is 2.000x" in terminal
    assert payload["cases"][0]["performance"]["reasons"] == performance.reasons


def test_error_reports_never_present_a_behavioural_diagnosis(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    error = ExampleResult(
        source="campaign",
        status=Status.ERROR,
        mismatches=[
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="one implementation raised and the other returned",
                path="$candidate.runtime",
            )
        ],
    )
    suite.status = Status.ERROR
    suite.cases[0] = suite.cases[0].model_copy(update={"status": Status.ERROR, "failures": [error]})

    json_report = render_json(suite)
    markdown = render_markdown(suite)
    terminal = render_terminal(suite)
    combined = (json_report + markdown + terminal).casefold()

    assert json.loads(json_report)["cases"][0]["diagnoses"] == []
    assert "behaviour differs" not in combined
    assert "behavior differs" not in combined
    assert "runtime preflight could not be completed" in combined


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "performance measurement runner is unavailable",
            "performance runner is unavailable; rerun with --no-performance or configure a "
            "supported runner",
        ),
        (
            "performance measurement has no validated representative input",
            "performance needs a validated input; add a fixture or generated schema, or rerun "
            "with --no-performance",
        ),
        (
            "candidate warmup benchmark did not return successfully "
            "(private_package.Customer42Error)",
            "candidate warmup benchmark execution failed; rerun with --no-performance to verify "
            "semantics separately",
        ),
        (
            "enforced memory gate requires peak RSS evidence for every paired run "
            "(2/5 pairs observed)",
            "enforced memory gate requires peak RSS evidence for every paired run "
            "(2/5 pairs observed); rerun with --no-performance to verify semantics separately",
        ),
    ],
)
def test_performance_errors_report_actionable_data_safe_diagnostics(
    message: str,
    expected: str,
) -> None:
    case = CaseResult(
        name="migration",
        status=Status.ERROR,
        failures=[
            ExampleResult(
                source="performance",
                status=Status.ERROR,
                mismatches=[
                    Mismatch(
                        kind=MismatchKind.PERFORMANCE,
                        message=message,
                        path="$performance",
                    )
                ],
            )
        ],
    )
    suite = SuiteResult(status=Status.ERROR, cases=[case])

    terminal = render_terminal(suite)
    payload = json.loads(render_json(suite))

    assert expected in terminal
    assert payload["cases"][0]["failures"][0]["mismatches"][0]["summary"] == expected
    assert "private_package" not in terminal
    assert "Customer42Error" not in terminal


def test_unknown_performance_error_remains_redacted_but_actionable() -> None:
    secret = "enforced memory gate requires customer@example.test"
    case = CaseResult(
        name="migration",
        status=Status.ERROR,
        failures=[
            ExampleResult(
                source="performance",
                status=Status.ERROR,
                mismatches=[
                    Mismatch(
                        kind=MismatchKind.PERFORMANCE,
                        message=secret,
                        path="$performance",
                    )
                ],
            )
        ],
    )

    terminal = render_terminal(SuiteResult(status=Status.ERROR, cases=[case]))

    assert "performance measurement could not be completed; rerun with --no-performance" in terminal
    assert secret not in terminal


def test_reports_warn_when_finding_limit_was_reached(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    suite.cases[0].finding_limit_reached = True

    payload = json.loads(render_json(suite))
    markdown = render_markdown(suite)
    terminal = render_terminal(suite)

    assert payload["cases"][0]["finding_limit_reached"] is True
    assert "the finding limit was reached" in markdown
    assert "finding limit reached" in terminal
    assert "--max-findings <higher-number>" in markdown + terminal


def test_terminal_can_emit_to_console_like_object(tmp_path: Path) -> None:
    class Console:
        rendered = ""

        def print(self, value: str, *, end: str = "\n", markup: bool = True) -> None:
            assert markup is False
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
    github_path = write_report(suite, "github", tmp_path / "github" / "summary.md")
    terminal_path = write_report(suite, "terminal", tmp_path / "terminal" / "summary.txt")
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert ET.parse(junit_path).getroot().tag == "testsuite"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Parity")
    assert github_path.read_text(encoding="utf-8").startswith("# Parity")
    assert terminal_path.read_text(encoding="utf-8").startswith("Parity FAILED")


def test_report_writer_preserves_destination_and_cleans_temporary_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "nested" / "report.json"
    destination.parent.mkdir()
    destination.write_text("previous\n", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("destination unavailable")

    monkeypatch.setattr("parity.reporting.os.replace", fail_replace)
    with pytest.raises(OSError, match="destination unavailable"):
        write_report(_suite(tmp_path), "json", destination)

    assert destination.read_text(encoding="utf-8") == "previous\n"
    assert list(destination.parent.glob(".report.json.*")) == []


def test_json_orders_signed_findings_by_signature_then_unsigned_errors(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    first = suite.cases[0].failures[0]
    first.finding_signature = "ms3:" + "f" * 64
    second = first.model_copy(
        update={
            "source": "second",
            "finding_signature": "ms3:" + "a" * 64,
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
        "ms3:" + "a" * 64,
        "ms3:" + "f" * 64,
        None,
    ]


def test_markdown_lists_every_distinct_artifact_deterministically(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    first = suite.cases[0].failures[0]
    first.artifact = tmp_path / "artifact-root" / "orders" / "z-campaign"
    second = first.model_copy(
        update={
            "finding_signature": "ms3:" + "a" * 64,
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

    second = failure.model_copy(update={"finding_signature": "ms3:" + "c" * 64})
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
