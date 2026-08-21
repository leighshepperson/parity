from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from parity import __version__, cli
from parity.migration import (
    MigrationCaseEvidence,
    MigrationCaseStatus,
    MigrationManifest,
    MigrationResult,
    MigrationUnit,
    MigrationUnitResult,
    MigrationUnitStatus,
)
from parity.migration_workspace import LaneMigrationResult, WorkspaceRunResult
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseResult,
    ColumnSchema,
    ExampleResult,
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


def test_migration_check_runs_complete_gate_and_writes_json(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    manifest = MigrationManifest(
        units=[
            MigrationUnit(id="orders-api", cases=["orders"]),
            MigrationUnit(id="plotting", excluded_reason="figure output"),
        ]
    )
    calls: list[set[str] | None] = []

    monkeypatch.setattr("parity.migration.load_config", lambda _path: config)
    monkeypatch.setattr("parity.migration.load_migration_manifest", lambda _path: manifest)

    def fake_run_suite(_config: ParityConfig, *, selected_cases=None) -> SuiteResult:
        calls.append(selected_cases)
        return _suite(Status.PASSED)

    monkeypatch.setattr("parity.engine.run_suite", fake_run_suite)
    monkeypatch.chdir(tmp_path)
    output = Path("reports/migration.json")

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "check",
            "--manifest",
            str(tmp_path / "migration.toml"),
            "--config",
            str(tmp_path / "parity.toml"),
            "--json",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "wrote reports/migration.json" in result.stdout
    assert calls == [{"orders"}]
    assert "all declared in-scope migration units passed" in result.stdout
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["summary"] == {
        "total": 2,
        "passed": 1,
        "failed": 0,
        "error": 0,
        "excluded": 1,
        "uncovered": 0,
    }


def test_migration_check_json_write_failure_is_operational_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("parity.migration.load_config", lambda _path: _config(tmp_path))
    monkeypatch.setattr(
        "parity.migration.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="orders-api", cases=["orders"])]),
    )
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(Status.PASSED),
    )

    result = runner.invoke(
        cli.app,
        ["migration", "check", "--json", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "migration report could not be written" in result.stderr
    assert "all declared in-scope migration units passed" not in result.stdout
    assert str(tmp_path) not in result.output


def test_migration_check_uses_exit_one_for_uncovered_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parity.migration.load_config", lambda _path: _config(tmp_path))
    monkeypatch.setattr(
        "parity.migration.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="customers")]),
    )
    selected: list[set[str] | None] = []

    def fake_run_suite(_config: ParityConfig, *, selected_cases=None) -> SuiteResult:
        selected.append(selected_cases)
        return SuiteResult(status=Status.PASSED, cases=[])

    monkeypatch.setattr("parity.engine.run_suite", fake_run_suite)

    result = runner.invoke(cli.app, ["migration", "check"])

    assert result.exit_code == 1
    assert selected == [set()]
    assert "migration incomplete" in result.stdout
    assert "uncovered" in result.stdout


def test_migration_check_rejects_unknown_case_before_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parity.migration.load_config", lambda _path: _config(tmp_path))
    monkeypatch.setattr(
        "parity.migration.load_migration_manifest",
        lambda _path: MigrationManifest(
            units=[MigrationUnit(id="customers", cases=["unknown-case"])]
        ),
    )
    called = False

    def fake_run_suite(*_args, **_kwargs) -> SuiteResult:
        nonlocal called
        called = True
        return _suite(Status.PASSED)

    monkeypatch.setattr("parity.engine.run_suite", fake_run_suite)

    result = runner.invoke(cli.app, ["migration", "check"])

    assert result.exit_code == 2
    assert "unknown case" in result.stderr
    assert not called


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [(Status.FAILED, 1), (Status.ERROR, 2)],
)
def test_migration_check_preserves_failure_and_error_exit_codes(
    tmp_path: Path,
    monkeypatch,
    status: Status,
    exit_code: int,
) -> None:
    monkeypatch.setattr("parity.migration.load_config", lambda _path: _config(tmp_path))
    monkeypatch.setattr(
        "parity.migration.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="orders-api", cases=["orders"])]),
    )
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(status),
    )

    result = runner.invoke(cli.app, ["migration", "check"])

    assert result.exit_code == exit_code
    assert "case evidence" in result.stdout
    assert f"Parity {status.value.upper()}" in result.stdout


def test_migration_run_json_emits_one_data_safe_lane_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    suite = _suite(Status.FAILED)
    migration = MigrationResult(
        status=Status.FAILED,
        units=[
            MigrationUnitResult(
                id="orders-api",
                status=MigrationUnitStatus.FAILED,
                cases=[
                    MigrationCaseEvidence(
                        name="orders",
                        status=MigrationCaseStatus.FAILED,
                        examples_run=1,
                    )
                ],
            )
        ],
        suite=suite,
        manifest_sha256="a" * 64,
    )
    completed = WorkspaceRunResult(
        lanes=(
            LaneMigrationResult(
                name="default",
                result=migration,
                report=tmp_path / "reports/default.json",
            ),
        )
    )
    monkeypatch.setattr(
        "parity.migration_workspace.run_workspace",
        lambda *_args, **_kwargs: completed,
    )

    result = runner.invoke(cli.app, ["migration", "run", "--json"])

    assert result.exit_code == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == "migration.run"
    assert payload["status"] == "failed"
    assert payload["reports"][0]["lane"] == "default"
    assert payload["result"]["lanes"][0]["report"]["schema_version"] == 1
    assert "dependency lane" not in result.stdout


def test_migration_run_human_output_reports_paths_from_invocation_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    artifact = project / "migrations/.parity/orders/campaign"
    suite = _suite(Status.FAILED)
    suite.cases[0].failures = [
        ExampleResult(
            source="generated",
            status=Status.FAILED,
            artifact=artifact,
            finding_signature="ms3:" + "a" * 64,
        )
    ]
    migration = MigrationResult(
        status=Status.FAILED,
        units=[
            MigrationUnitResult(
                id="orders-api",
                status=MigrationUnitStatus.FAILED,
                cases=[
                    MigrationCaseEvidence(
                        name="orders",
                        status=MigrationCaseStatus.FAILED,
                        examples_run=1,
                    )
                ],
            )
        ],
        suite=suite,
        manifest_sha256="a" * 64,
    )
    report = project / "migrations/.parity/workspace/reports/default.json"
    source_provenance = report.with_name("source-provenance.json")
    completed = WorkspaceRunResult(
        lanes=(LaneMigrationResult(name="default", result=migration, report=report),),
        source_provenance=source_provenance,
    )
    monkeypatch.setattr(
        "parity.migration_workspace.run_workspace",
        lambda *_args, **_kwargs: completed,
    )
    invocation = tmp_path / "unrelated"
    invocation.mkdir()
    monkeypatch.chdir(invocation)

    result = runner.invoke(cli.app, ["migration", "run"])

    assert result.exit_code == 1, result.output
    expected_report = report.relative_to(tmp_path).as_posix()
    expected_source = source_provenance.relative_to(tmp_path).as_posix()
    expected_artifact = artifact.relative_to(tmp_path).as_posix()
    assert f"report ../{expected_report}" in result.stdout
    assert "source provenance" in result.stdout
    assert f"../{expected_source}" in result.stdout
    assert f"artifact: ../{expected_artifact}" in result.stdout


def test_migration_run_json_only_advertises_executable_replay_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replayable = tmp_path / "artifacts/replayable"
    evidence_only = tmp_path / "artifacts/evidence-only"
    legacy = tmp_path / "artifacts/legacy"
    for artifact in (replayable, evidence_only, legacy):
        artifact.mkdir(parents=True)
    (replayable / "replay.json").write_text(
        json.dumps(
            {
                "version": 2,
                "path_base": {"kind": "artifact_ancestor", "levels": 2},
                "command": ["parity", "replay", "<artifact-path>"],
            }
        ),
        encoding="utf-8",
    )
    (evidence_only / "replay.json").write_text(
        json.dumps(
            {
                "version": 2,
                "path_base": {"kind": "artifact_ancestor", "levels": 2},
                "replay_blockers": {"reference": "live_callable"},
            }
        ),
        encoding="utf-8",
    )
    (legacy / "replay.json").write_text(
        json.dumps(
            {
                "version": 1,
                "path_base": {"kind": "artifact_ancestor", "levels": 2},
                "command": ["parity", "replay", "<artifact-path>"],
            }
        ),
        encoding="utf-8",
    )
    suite = _suite(Status.FAILED)
    suite.cases[0].failures = [
        ExampleResult(
            source="generated",
            status=Status.FAILED,
            artifact=replayable,
            finding_signature="ms3:" + "a" * 64,
        ),
        ExampleResult(
            source="generated",
            status=Status.FAILED,
            artifact=evidence_only,
            finding_signature="ms3:" + "b" * 64,
        ),
        ExampleResult(
            source="generated",
            status=Status.FAILED,
            artifact=legacy,
            finding_signature="ms3:" + "c" * 64,
        ),
    ]
    migration = MigrationResult(
        status=Status.FAILED,
        units=[
            MigrationUnitResult(
                id="orders-api",
                status=MigrationUnitStatus.FAILED,
                cases=[
                    MigrationCaseEvidence(
                        name="orders",
                        status=MigrationCaseStatus.FAILED,
                        examples_run=3,
                    )
                ],
            )
        ],
        suite=suite,
        manifest_sha256="a" * 64,
    )
    completed = WorkspaceRunResult(
        lanes=(
            LaneMigrationResult(
                name="default",
                result=migration,
                report=tmp_path / "reports/default.json",
            ),
        )
    )
    monkeypatch.setattr(
        "parity.migration_workspace.run_workspace",
        lambda *_args, **_kwargs: completed,
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["migration", "run", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    artifacts = {item["path"]: item for item in payload["artifacts"]}
    assert artifacts["artifacts/replayable"]["replay_command"] == {
        "argv": ["parity", "replay", "artifacts/replayable", "--json"],
        "cwd": "invocation",
    }
    assert artifacts["artifacts/evidence-only"]["replay_command"] is None
    assert artifacts["artifacts/legacy"]["replay_command"] is None
    assert payload["issues"] == [
        {
            "code": "artifact.evidence_only",
            "severity": "warning",
            "message": "artifact is retained evidence but has no executable replay contract",
            "path": "artifacts/evidence-only",
            "case": "orders",
            "side": None,
        },
        {
            "code": "artifact.evidence_only",
            "severity": "warning",
            "message": "artifact is retained evidence but has no executable replay contract",
            "path": "artifacts/legacy",
            "case": "orders",
            "side": None,
        },
    ]


def test_version_and_init_are_runnable(tmp_path: Path) -> None:
    version = runner.invoke(cli.app, ["version"])
    assert version.exit_code == 0
    assert version.stdout.strip() == __version__

    version_option = runner.invoke(cli.app, ["--version"])
    assert version_option.exit_code == 0
    assert version_option.stdout.strip() == __version__

    config_path = tmp_path / "nested" / "parity.toml"
    created = runner.invoke(cli.app, ["init", str(config_path)])
    assert created.exit_code == 0
    assert f"next: parity check --config {config_path.as_posix()}" in created.stdout
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
    assert f"next: parity check --config {config_path.as_posix()}" in result.stdout
    assert not (config_path.parent / "parity_example.py").exists()
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert raw["cases"][0]["reference"]["target"] == raw["cases"][0]["candidate"]["target"]
    assert raw["cases"][0]["comparison"] == {"row_order": "keyed", "row_keys": ["id"]}
    configured = cli.load_config(config_path)
    assert configured.cases[0].fixture == fixture.resolve()


def test_default_init_prints_the_immediate_next_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "next: parity check"


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


def test_inspect_creates_output_parents_and_handles_write_errors(tmp_path: Path) -> None:
    fixture = tmp_path / "input.parquet"
    pq.write_table(pa.table({"id": [1]}), fixture)
    nested = tmp_path / "nested" / "schema.json"

    written = runner.invoke(cli.app, ["inspect", str(fixture), "--output", str(nested)])
    assert written.exit_code == 0, written.output
    assert nested.is_file()
    assert "wrote" in written.stdout

    failed = runner.invoke(cli.app, ["inspect", str(fixture), "--output", str(tmp_path)])
    assert failed.exit_code == 2
    assert "schema output could not be written" in failed.stderr
    assert "Traceback" not in failed.output


def test_doctor_config_reports_workers_side_by_side_and_filters_case(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "cli_doctor_target.py").write_text(
        "def run(frame):\n    return frame\n",
        encoding="utf-8",
    )
    config = ParityConfig(
        cases=[
            CaseConfig(
                name=name,
                reference=CallableSpec(
                    target="cli_doctor_target:run",
                    python=Path(sys.executable),
                    workdir=tmp_path,
                    record_distributions=["pytest"],
                ),
                candidate=CallableSpec(
                    target="cli_doctor_target:run",
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
    assert "cli_doctor_target" not in result.stdout

    terminal = runner.invoke(
        cli.app,
        ["doctor", "--config", str(tmp_path / "parity.toml"), "--case", "orders"],
    )
    assert terminal.exit_code == 0
    assert "Reference" in terminal.stdout
    assert "Candidate" in terminal.stdout
    assert "Python" in terminal.stdout
    assert "Parity" in terminal.stdout
    assert "target runtimes and imports ready; targets were not invoked" in terminal.stdout


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
    result = runner.invoke(cli.app, ["doctor", "--case", "package[extra]"])
    assert result.exit_code == 2
    assert "--case requires --config" in result.stderr
    assert "package[extra]" not in result.stderr


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
    monkeypatch.chdir(tmp_path)
    json_path = Path("reports/report.json")
    junit_path = Path("reports/junit.xml")
    markdown_path = Path("reports/summary.md")
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
            "--jobs",
            "3",
            "--native-threads",
            "2",
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
    assert config.jobs == 3
    assert config.native_threads == 2
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert "<testsuite" in junit_path.read_text(encoding="utf-8")
    assert markdown_path.read_text(encoding="utf-8").startswith("# Parity verification")
    assert result.stdout.count("wrote") == 3
    assert "wrote reports/report.json" in result.stdout
    assert "wrote reports/junit.xml" in result.stdout
    assert "wrote reports/summary.md" in result.stdout


def test_check_markdown_alone_creates_parents_and_write_failures_exit_two(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _path: _config(tmp_path))
    monkeypatch.setattr("parity.engine.run_suite", lambda *_args, **_kwargs: _suite(Status.PASSED))
    markdown = tmp_path / "deep" / "reports" / "summary.md"

    written = runner.invoke(cli.app, ["check", "--markdown", str(markdown)])
    assert written.exit_code == 0, written.output
    assert markdown.read_text(encoding="utf-8").startswith("# Parity verification")
    assert "wrote" in written.stdout

    failed = runner.invoke(cli.app, ["check", "--json", str(tmp_path)])
    assert failed.exit_code == 2
    assert "verification report could not be written" in failed.stderr
    assert "Traceback" not in failed.output


def test_check_rejects_unknown_case_and_appends_github_summary(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    unknown = runner.invoke(cli.app, ["check", "--case", "missing"])
    assert unknown.exit_code == 2
    assert "unknown case" in unknown.stderr

    unknown_tag = runner.invoke(cli.app, ["check", "--tag", "package[extra]"])
    assert unknown_tag.exit_code == 2
    assert "unknown tag" in unknown_tag.stderr
    assert "package[extra]" in unknown_tag.stderr

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


def test_replay_prints_artifact_path_relative_to_invocation(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "project/artifacts/orders/finding"
    artifact.mkdir(parents=True)
    invocation = tmp_path / "unrelated"
    invocation.mkdir()
    suite = SuiteResult(
        status=Status.FAILED,
        cases=[
            CaseResult(
                name="orders",
                status=Status.FAILED,
                examples_run=1,
                failures=[
                    ExampleResult(
                        source="replay",
                        status=Status.FAILED,
                        artifact=artifact,
                        finding_signature="ms3:" + "a" * 64,
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _path: suite)
    monkeypatch.chdir(invocation)

    result = runner.invoke(
        cli.app,
        ["replay", "../project/artifacts/orders/finding"],
    )

    assert result.exit_code == 1
    rendered = "../project/artifacts/orders/finding"
    assert f"artifact: {rendered}" in result.stdout
    assert (invocation / rendered).resolve() == artifact.resolve()


def test_replay_json_is_one_data_safe_document_for_results_and_errors(
    tmp_path: Path, monkeypatch
) -> None:
    artifact = tmp_path / "campaign"
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _path: _suite(Status.FAILED))

    failed = runner.invoke(cli.app, ["replay", str(artifact), "--json"])

    assert failed.exit_code == 1
    assert failed.stderr == ""
    payload = json.loads(failed.stdout)
    assert payload["command"] == "replay"
    assert payload["status"] == "failed"
    assert payload["result"]["schema_version"] == 3

    def unavailable(_path: Path) -> SuiteResult:
        raise RuntimeError("artifact is unavailable")

    monkeypatch.setattr("parity.engine.replay_artifact", unavailable)
    errored = runner.invoke(cli.app, ["replay", str(artifact), "--json"])
    assert errored.exit_code == 2
    error_payload = json.loads(errored.stdout)
    assert error_payload["status"] == "error"
    assert error_payload["issues"][0]["code"] == "operational_error"


def test_schema_command_lists_and_emits_versioned_contracts() -> None:
    listed = runner.invoke(cli.app, ["schema", "list"])
    assert listed.exit_code == 0
    assert "workspace" in json.loads(listed.stdout)["contracts"]

    workspace = runner.invoke(cli.app, ["schema", "workspace"])
    assert workspace.exit_code == 0
    schema = json.loads(workspace.stdout)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/workspace/v3.json")
    assert schema["properties"]["version"]["const"] == 3
