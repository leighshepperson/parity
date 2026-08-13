from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from typer.testing import CliRunner

from parity import cli
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseResult,
    ColumnSchema,
    FrameSchema,
    ParityConfig,
    Status,
    SuiteResult,
)

runner = CliRunner()


def _config(tmp_path: Path) -> ParityConfig:
    return ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="orders",
                reference=CallableSpec(target="old:transform", adapter="pandas"),
                candidate=CallableSpec(target="new:transform", adapter="polars"),
                input_schema=FrameSchema(columns=[ColumnSchema(name="id", dtype="int64")]),
                tags={"critical"},
            )
        ],
    )


def _suite(status: Status) -> SuiteResult:
    return SuiteResult(
        status=status,
        cases=[CaseResult(name="orders", status=status, examples_run=1)],
        elapsed_seconds=0.01,
    )


def test_version_and_init_are_runnable(tmp_path: Path) -> None:
    version = runner.invoke(cli.app, ["version"])
    assert version.exit_code == 0
    assert version.stdout.strip() == "0.6.0"

    config_path = tmp_path / "nested" / "parity.toml"
    created = runner.invoke(cli.app, ["init", str(config_path)])
    assert created.exit_code == 0
    assert config_path.is_file()
    assert (config_path.parent / "parity_example.py").is_file()

    refused = runner.invoke(cli.app, ["init", str(config_path)])
    assert refused.exit_code == 2
    assert "already exists" in refused.stderr


def test_inspect_and_doctor_commands(tmp_path: Path) -> None:
    fixture = tmp_path / "input.parquet"
    pq.write_table(pa.table({"id": [1, 2], "name": ["a", "b"]}), fixture)
    output = tmp_path / "schema.json"

    inspected = runner.invoke(cli.app, ["inspect", str(fixture), "--output", str(output)])
    assert inspected.exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [column["name"] for column in payload["columns"]] == ["id", "name"]

    doctor = runner.invoke(cli.app, ["doctor", "--json"])
    assert doctor.exit_code == 0
    assert all(item["installed"] for item in json.loads(doctor.stdout)["dependencies"])


def test_check_applies_filters_overrides_and_writes_safe_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    def run_suite(selected_config, *, selected_cases=None):
        captured["config"] = selected_config
        captured["cases"] = selected_cases
        return _suite(Status.FAILED)

    monkeypatch.setattr("parity.engine.run_suite", run_suite)
    json_path = tmp_path / "reports" / "report.json"
    junit_path = tmp_path / "reports" / "junit.xml"
    markdown_path = tmp_path / "reports" / "summary.md"
    result = runner.invoke(
        cli.app,
        [
            "check",
            "--case",
            "orders",
            "--tag",
            "critical",
            "--max-examples",
            "7",
            "--max-findings",
            "3",
            "--stability-repeats",
            "4",
            "--no-performance",
            "--json",
            str(json_path),
            "--junit",
            str(junit_path),
            "--markdown",
            str(markdown_path),
        ],
    )
    assert result.exit_code == 1
    assert captured["config"] is config
    assert captured["cases"] == {"orders"}
    assert config.cases[0].generation.max_examples == 7
    assert config.cases[0].generation.max_findings == 3
    assert config.cases[0].generation.stability_repeats == 4
    assert config.cases[0].performance.enabled is False
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert "<testsuite" in junit_path.read_text(encoding="utf-8")
    assert markdown_path.read_text(encoding="utf-8").startswith("# Parity verification")


def test_check_rejects_unknown_case_and_appends_github_summary(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    unknown = runner.invoke(cli.app, ["check", "--case", "missing"])
    assert unknown.exit_code == 2
    assert "unknown case" in unknown.stderr

    unknown_tag = runner.invoke(cli.app, ["check", "--tag", "does-not-exist"])
    assert unknown_tag.exit_code == 2
    assert "unknown tag" in unknown_tag.stderr

    summary = tmp_path / "step-summary.md"
    summary.write_text("existing\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr("parity.engine.run_suite", lambda *_args, **_kwargs: _suite(Status.PASSED))
    passed = runner.invoke(cli.app, ["check"])
    assert passed.exit_code == 0
    rendered = summary.read_text(encoding="utf-8")
    assert rendered.startswith("existing\n")
    assert "# Parity verification" in rendered


def test_replay_preserves_exit_contract(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "campaign"
    artifact.mkdir()
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _path: _suite(Status.ERROR))
    result = runner.invoke(cli.app, ["replay", str(artifact)])
    assert result.exit_code == 2
    assert "Parity ERROR" in result.stdout
