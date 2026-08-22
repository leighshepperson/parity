from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from parity import cli
from parity.compatibility import (
    CompatibilityBudgetError,
    approve_compatibility_finding,
    capture_compatibility_budget,
    load_compatibility_budget,
)
from parity.config import ConfigError, load_config
from parity.engine import run_suite
from parity.models import CompatibilityDecision, Status
from parity.reporting import write_report

runner = CliRunner()


def _write_arrow(path: Path) -> None:
    table = pa.table({"value": [1, 5]})
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)


def _write_project(project: Path, *, budget: bool, max_findings: int = 2) -> Path:
    (project / "target.py").write_text(
        """
import pyarrow.compute as pc

def reference(table):
    return table

def candidate(table):
    return table.set_column(0, "value", pc.add(table.column("value"), 1))
""",
        encoding="utf-8",
    )
    _write_arrow(project / "input.arrow")
    config = project / "parity.toml"
    budget_line = 'compatibility_budget = "compatibility.toml"\n' if budget else ""
    config.write_text(
        f"""
version = 1
{budget_line}
[[cases]]
name = "upgrade"
fixture = "input.arrow"

[cases.reference]
target = "target:reference"
adapter = "arrow"

[cases.candidate]
target = "target:candidate"
adapter = "arrow"

[cases.generation]
search = false
adversarial_examples = false
stability_repeats = 1
max_findings = {max_findings}

[cases.performance]
enabled = true
repeats = 1
min_reference_ms = 0
fail_on_regression = false
""",
        encoding="utf-8",
    )
    return config


def _capture_current_budget(project: Path) -> tuple[Path, str]:
    config = _write_project(project, budget=False)
    result = run_suite(load_config(config))
    assert result.status is Status.FAILED
    report = project / ".parity" / "report.json"
    write_report(result, "json", report)
    destination = project / "compatibility.toml"
    captured = capture_compatibility_budget(report, destination)
    signature = result.cases[0].failures[0].finding_signature
    assert signature is not None
    assert captured.findings == 1
    return destination, signature


@pytest.mark.integration
def test_approved_difference_passes_remains_visible_and_runs_performance(
    tmp_path: Path,
) -> None:
    budget_path, signature = _capture_current_budget(tmp_path)
    approve_compatibility_finding(
        budget_path,
        "upgrade",
        signature,
        reason="The new API deliberately increments this normalized field.",
    )
    config = _write_project(tmp_path, budget=True)

    result = run_suite(load_config(config))

    assert result.status is Status.PASSED
    case = result.cases[0]
    assert case.status is Status.PASSED
    assert case.failures[0].approved is True
    assert case.failures[0].finding_signature == signature
    assert case.compatibility is not None
    assert case.compatibility.approved_findings == [signature]
    assert case.compatibility.unapproved_findings == []
    assert case.performance is not None


@pytest.mark.integration
def test_new_difference_fails_and_does_not_consume_an_old_approval(tmp_path: Path) -> None:
    budget_path, signature = _capture_current_budget(tmp_path)
    approve_compatibility_finding(
        budget_path,
        "upgrade",
        signature,
        reason="Reviewed numeric change.",
    )
    config = _write_project(tmp_path, budget=True)
    (tmp_path / "target.py").write_text(
        """
def reference(table):
    return table

def candidate(table):
    return table.rename_columns(["renamed"])
""",
        encoding="utf-8",
    )
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)

    result = run_suite(load_config(config))

    assert result.status is Status.FAILED
    case = result.cases[0]
    assert case.failures[0].approved is False
    assert case.compatibility is not None
    assert case.compatibility.approved_findings == []
    assert case.compatibility.unused_approvals == [signature]
    assert case.compatibility.unapproved_findings == [case.failures[0].finding_signature]
    assert case.performance is None


def test_budget_capture_and_approve_cli_require_explicit_rationale(tmp_path: Path) -> None:
    budget_path, signature = _capture_current_budget(tmp_path)
    budget_path.unlink()
    report = tmp_path / ".parity" / "report.json"

    captured = runner.invoke(cli.app, ["budget", "init", str(report), str(budget_path)])
    assert captured.exit_code == 0, captured.output
    assert "1 finding(s) require review" in captured.output

    missing_reason = runner.invoke(
        cli.app,
        ["budget", "approve", str(budget_path), "upgrade", signature],
    )
    assert missing_reason.exit_code != 0

    approved = runner.invoke(
        cli.app,
        [
            "budget",
            "approve",
            str(budget_path),
            "upgrade",
            signature,
            "--reason",
            "Reviewed intentional contract change",
        ],
    )
    assert approved.exit_code == 0, approved.output
    finding = load_compatibility_budget(budget_path).findings[0]
    assert finding.decision is CompatibilityDecision.APPROVED
    assert finding.reason == "Reviewed intentional contract change"


def test_budget_cannot_hide_discovery_at_the_finding_limit(tmp_path: Path) -> None:
    budget_path, signature = _capture_current_budget(tmp_path)
    approve_compatibility_finding(
        budget_path,
        "upgrade",
        signature,
        reason="Reviewed intentional contract change",
    )
    config = _write_project(tmp_path, budget=True, max_findings=1)

    with pytest.raises(ConfigError, match="must exceed its approved"):
        load_config(config)


def test_cli_finding_override_cannot_hide_discovery(tmp_path: Path) -> None:
    budget_path, signature = _capture_current_budget(tmp_path)
    approve_compatibility_finding(
        budget_path,
        "upgrade",
        signature,
        reason="Reviewed intentional contract change",
    )
    config = _write_project(tmp_path, budget=True, max_findings=2)

    result = runner.invoke(
        cli.app,
        [
            "check",
            "--config",
            str(config),
            "--max-findings",
            "1",
            "--no-performance",
        ],
    )

    assert result.exit_code == 2
    assert "must exceed its approved" in result.output


def test_budget_rejects_unsigned_operational_reports(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "status": "error",
                "cases": [
                    {
                        "name": "upgrade",
                        "status": "error",
                        "examples_run": 0,
                        "deterministic_examples": 0,
                        "generated_examples": 0,
                        "findings_discovered": 0,
                        "finding_limit_reached": False,
                        "failures": [
                            {
                                "source": "campaign",
                                "status": "error",
                                "finding_signature": None,
                                "approved": False,
                                "mismatch_counts": {"exception": 1},
                                "mismatches": [
                                    {
                                        "kind": "exception",
                                        "summary": "Parity could not perform the comparison",
                                        "path": "$",
                                    }
                                ],
                                "artifact": None,
                                "reference_metrics": None,
                                "candidate_metrics": None,
                            }
                        ],
                        "diagnoses": [],
                        "performance": None,
                        "provenance": None,
                        "compatibility": None,
                        "elapsed_seconds": 0,
                    }
                ],
                "elapsed_seconds": 0,
                "parity_version": "0.18.0",
                "provenance": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CompatibilityBudgetError, match="operational or unsigned"):
        capture_compatibility_budget(report, tmp_path / "compatibility.toml")


def test_config_rejects_a_budget_outside_its_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_project(project, budget=False)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "version = 1",
            'version = 1\ncompatibility_budget = "../outside.toml"',
        ),
        encoding="utf-8",
    )
    (tmp_path / "outside.toml").write_text("version = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="must stay within"):
        load_config(config)
