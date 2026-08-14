from __future__ import annotations

import json
import sys
import tomllib
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
    assert version.stdout.strip() == "0.8.1"

    config_path = tmp_path / "nested" / "parity.toml"
    created = runner.invoke(cli.app, ["init", str(config_path)])
    assert created.exit_code == 0
    assert config_path.is_file()
    assert (config_path.parent / "parity_example.py").is_file()

    refused = runner.invoke(cli.app, ["init", str(config_path)])
    assert refused.exit_code == 2
    assert "already exists" in refused.stderr


def test_init_project_mode_writes_only_a_runnable_config(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "input.parquet"
    fixture.parent.mkdir()
    pq.write_table(pa.table({"id": [1, 2]}), fixture)
    config_path = tmp_path / "config" / "parity.toml"
    result = runner.invoke(
        cli.app,
        [
            "init",
            str(config_path),
            "--reference",
            "project.transform:run",
            "--candidate",
            "project.transform:run",
            "--fixture",
            str(fixture),
            "--case-name",
            "polars-versions",
            "--reference-adapter",
            "polars",
            "--candidate-adapter",
            "polars",
            "--reference-python",
            sys.executable,
            "--candidate-python",
            sys.executable,
            "--record-distribution",
            "polars",
            "--row-key",
            "id",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (config_path.parent / "parity_example.py").exists()
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert raw["cases"][0]["reference"]["target"] == raw["cases"][0]["candidate"]["target"]
    assert raw["cases"][0]["comparison"] == {"row_order": "keyed", "row_keys": ["id"]}
    configured = cli.load_config(config_path)
    assert configured.cases[0].fixture == fixture.resolve()


def test_init_project_mode_requires_the_target_and_fixture_trio(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["init", str(tmp_path / "parity.toml"), "--reference", "project.old:run"],
    )
    assert result.exit_code == 2
    assert "must be provided together" in result.stderr
    assert not (tmp_path / "parity.toml").exists()

    option_without_trio = runner.invoke(
        cli.app,
        ["init", str(tmp_path / "parity.toml"), "--row-key", "id"],
    )
    assert option_without_trio.exit_code == 2
    assert "require --reference" in option_without_trio.stderr


def test_init_project_mode_rejects_bad_adapter_and_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "input.parquet"
    pq.write_table(pa.table({"id": [1]}), fixture)
    common = [
        "init",
        str(tmp_path / "parity.toml"),
        "--reference",
        "project.old:run",
        "--candidate",
        "project.new:run",
        "--fixture",
        str(fixture),
    ]
    adapter = runner.invoke(cli.app, [*common, "--reference-adapter", "spark"])
    assert adapter.exit_code == 2
    assert "reference_adapter must be one of" in adapter.stderr

    for target in ("pkg..module:run", "pkg:run²", "pkg:run¼"):
        malformed = runner.invoke(cli.app, [*common, "--reference", target])
        assert malformed.exit_code == 2
        assert "reference must be an import target" in malformed.stderr

    fixture.unlink()
    missing = runner.invoke(cli.app, common)
    assert missing.exit_code == 2
    assert "fixture not found" in missing.stderr
    assert not (tmp_path / "parity.toml").exists()


def test_init_project_mode_output_runs_without_generated_demo(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project_transform.py").write_text(
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    fixture = project / "input.parquet"
    pq.write_table(pa.table({"id": [1, 2]}), fixture)
    config_path = project / "parity.toml"
    monkeypatch.chdir(project)
    initialized = runner.invoke(
        cli.app,
        [
            "init",
            str(config_path),
            "--reference",
            "project_transform:transform",
            "--candidate",
            "project_transform:transform",
            "--fixture",
            str(fixture),
            "--reference-adapter",
            "arrow",
            "--candidate-adapter",
            "arrow",
        ],
    )
    assert initialized.exit_code == 0, initialized.output

    checked = runner.invoke(
        cli.app,
        [
            "check",
            "--config",
            str(config_path),
            "--max-examples",
            "2",
            "--stability-repeats",
            "1",
            "--no-performance",
        ],
    )
    assert checked.exit_code == 0, checked.output


def test_init_nested_project_config_runs_targets_from_invocation_root(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    migrations = project / "migrations"
    migrations.mkdir(parents=True)
    (project / "project_transform.py").write_text(
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    fixture = project / "input.parquet"
    pq.write_table(pa.table({"id": [1, 2]}), fixture)
    config_path = migrations / "parity.toml"
    monkeypatch.chdir(project)

    initialized = runner.invoke(
        cli.app,
        [
            "init",
            str(config_path),
            "--reference",
            "project_transform:transform",
            "--candidate",
            "project_transform:transform",
            "--fixture",
            str(fixture),
            "--reference-adapter",
            "arrow",
            "--candidate-adapter",
            "arrow",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))["cases"][0]
    assert raw["reference"]["workdir"] == ".."
    assert raw["candidate"]["workdir"] == ".."

    checked = runner.invoke(
        cli.app,
        [
            "check",
            "--config",
            str(config_path),
            "--max-examples",
            "2",
            "--stability-repeats",
            "1",
            "--no-performance",
        ],
    )
    assert checked.exit_code == 0, checked.output


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


def test_doctor_config_reports_workers_side_by_side_and_filters_case(
    tmp_path: Path, monkeypatch
) -> None:
    config = ParityConfig(
        cases=[
            CaseConfig(
                name=name,
                reference=CallableSpec(
                    target="missing.reference:run",
                    python=Path(sys.executable),
                    workdir=tmp_path,
                    record_distributions=["pytest"],
                ),
                candidate=CallableSpec(
                    target="missing.candidate:run",
                    python=Path(sys.executable),
                    workdir=tmp_path,
                    record_distributions=["pytest"],
                ),
                fixture=tmp_path / "unused.json",
            )
            for name in ("orders", "customers")
        ]
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    result = runner.invoke(
        cli.app,
        ["doctor", "--config", str(tmp_path / "parity.toml"), "--case", "orders", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["healthy"] is True
    assert [case["name"] for case in payload["cases"]] == ["orders"]
    assert payload["cases"][0]["reference"]["distributions"][0]["name"] == "pytest"
    assert str(tmp_path) not in result.stdout
    assert sys.executable not in result.stdout
    assert "missing.reference" not in result.stdout

    terminal = runner.invoke(
        cli.app,
        ["doctor", "--config", str(tmp_path / "parity.toml"), "--case", "orders"],
    )
    assert terminal.exit_code == 0
    assert "Reference" in terminal.stdout
    assert "Candidate" in terminal.stdout
    assert "Python" in terminal.stdout
    assert "Parity" in terminal.stdout


def test_doctor_config_uses_exit_two_for_missing_distribution(tmp_path: Path, monkeypatch) -> None:
    spec = CallableSpec(
        target="missing.target:run",
        python=Path(sys.executable),
        workdir=tmp_path,
        record_distributions=["parity-package-does-not-exist"],
    )
    config = ParityConfig(
        cases=[
            CaseConfig(
                name="orders",
                reference=spec,
                candidate=spec.model_copy(deep=True),
                fixture=tmp_path / "unused.json",
            )
        ]
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    result = runner.invoke(cli.app, ["doctor", "--config", str(tmp_path / "parity.toml"), "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["healthy"] is False
    assert payload["cases"][0]["reference"]["distributions"][0]["status"] == "missing"


def test_doctor_case_requires_config() -> None:
    result = runner.invoke(cli.app, ["doctor", "--case", "orders"])
    assert result.exit_code == 2
    assert "--case requires --config" in result.stderr


def test_doctor_config_load_error_does_not_echo_paths_or_values(
    tmp_path: Path, monkeypatch
) -> None:
    secret_path = tmp_path / "private" / "python"
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: (_ for _ in ()).throw(
            cli.ConfigError(f"invalid python={secret_path} environment=PRIVATE_TOKEN=secret")
        ),
    )
    result = runner.invoke(
        cli.app, ["doctor", "--config", str(tmp_path / "private" / "parity.toml"), "--json"]
    )
    assert result.exit_code == 2
    assert "could not be loaded or validated" in result.stderr
    assert str(tmp_path) not in result.stderr
    assert "PRIVATE_TOKEN" not in result.stderr
    assert "secret" not in result.stderr


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
