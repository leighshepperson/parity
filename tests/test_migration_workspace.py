from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from parity import __version__, cli
from parity.migration import MigrationManifest, MigrationResult, MigrationUnit
from parity.migration_workspace import (
    LaneEnvironment,
    MigrationWorkspace,
    ResolvedWorkspace,
    ResolvedWorkspaceLane,
    WorkspaceError,
    WorkspaceLane,
    WorkspaceSetup,
    load_workspace,
    parse_lane_options,
    render_tox_config,
    run_workspace,
    setup_workspace,
    write_workspace,
)
from parity.models import (
    CallableSpec,
    CaseConfig,
    ColumnSchema,
    FrameSchema,
    ParityConfig,
)

runner = CliRunner()


def _project(tmp_path: Path, *, lanes: tuple[WorkspaceLane, ...] = ()) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "parity.toml").write_text("", encoding="utf-8")
    (tmp_path / "migration.toml").write_text("", encoding="utf-8")
    workspace = tmp_path / "parity.workspace.toml"
    write_workspace(
        workspace,
        reference="candidate-lib==1.9.0",
        candidate=Path("."),
        python_version="3.12",
        lanes=lanes,
    )
    return workspace


def _config() -> ParityConfig:
    return ParityConfig(
        cases=[
            CaseConfig(
                name="orders",
                reference=CallableSpec(target="candidate_lib:transform"),
                candidate=CallableSpec(target="candidate_lib:transform"),
                input_schema=FrameSchema(columns=[ColumnSchema(name="id", dtype="int64")]),
            )
        ]
    )


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_workspace_model_rejects_ambiguous_inputs_and_parses_lanes() -> None:
    with pytest.raises(ValidationError, match="exact requirement"):
        MigrationWorkspace(reference="candidate-lib>=1", python="3.12")
    for invalid in ("candidate-lib==banana", "candidate-lib==1..2", "candidate-lib[-]==1"):
        with pytest.raises(ValidationError, match="exact requirement"):
            MigrationWorkspace(reference=invalid, python="3.12")
    with pytest.raises(ValidationError, match=r"Python >=3\.11"):
        MigrationWorkspace(reference="candidate-lib==1", python="3.10")
    with pytest.raises(ValidationError, match="lane names must be unique"):
        MigrationWorkspace(
            reference="candidate-lib==1",
            python="3.12",
            lanes=[WorkspaceLane(name="same"), WorkspaceLane(name="same")],
        )

    lanes = parse_lane_options(["release=release.in", "current=current.in"])

    assert lanes == [
        WorkspaceLane(name="release", requirements=Path("release.in")),
        WorkspaceLane(name="current", requirements=Path("current.in")),
    ]
    with pytest.raises(WorkspaceError, match="must be unique"):
        parse_lane_options(["same", "same"])


def test_workspace_paths_are_relative_to_document_and_candidate_must_be_local(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    candidate = project / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    requirements = project / "requirements-current.in"
    requirements.write_text("pandas<3\n", encoding="utf-8")
    checks = project / "checks"
    checks.mkdir()
    (checks / "parity.toml").write_text("", encoding="utf-8")
    (checks / "migration.toml").write_text("", encoding="utf-8")
    workspace_path = project / "parity.workspace.toml"
    write_workspace(
        workspace_path,
        reference="candidate-lib[plot,io]==1.9.0",
        candidate=Path("candidate"),
        python_version="3.13",
        config=Path("checks/parity.toml"),
        manifest=Path("checks/migration.toml"),
        lanes=[WorkspaceLane(name="current", requirements=Path("requirements-current.in"))],
    )

    (candidate / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    workspace = load_workspace(workspace_path)

    assert workspace.candidate == candidate.resolve()
    assert workspace.config == (project / "checks/parity.toml").resolve()
    assert workspace.manifest == (project / "checks/migration.toml").resolve()
    assert workspace.reference_extras == ("plot", "io")
    assert workspace.lanes[0].requirements == requirements.resolve()

    (candidate / "pyproject.toml").unlink()
    with pytest.raises(WorkspaceError, match="does not fetch or modify source"):
        load_workspace(workspace_path)


def test_workspace_rejects_candidate_with_wrong_or_unverifiable_distribution_name(
    tmp_path: Path,
) -> None:
    workspace_path = _project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "other-candidate"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="does not match reference distribution"):
        load_workspace(workspace_path)

    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (tmp_path / "setup.py").write_text("# dynamic legacy metadata\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="is not declared statically"):
        load_workspace(workspace_path)


def test_generated_tox_config_pairs_every_lane_and_reuses_its_lock(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    workspace = ResolvedWorkspace(
        path=project / "parity.workspace.toml",
        root=project,
        reference="candidate-lib[io]==1.9.0",
        reference_extras=("io",),
        candidate=project,
        candidate_package="editable",
        python="3.12",
        config=project / "parity.toml",
        manifest=project / "migration.toml",
        report_dir=project / ".parity/workspace/reports",
        lanes=(
            ResolvedWorkspaceLane("release", None),
            ResolvedWorkspaceLane("current", None),
        ),
    )

    rendered = render_tox_config(workspace, state_root=project / ".parity" / "workspace")
    parsed = tomllib.loads(rendered)

    assert parsed["requires"] == ["tox>=4.44", "tox-uv>=1.29", "uv>=0.9.1"]
    assert parsed["env_list"] == [
        "release-reference",
        "release-candidate",
        "current-reference",
        "current-candidate",
    ]
    assert parsed["env"]["release-reference"]["package"] == "skip"
    assert parsed["env"]["release-candidate"]["package"] == "editable"
    assert parsed["env"]["release-candidate"]["extras"] == ["io"]
    assert parsed["env"]["release-reference"]["deps"] == parsed["env"]["release-candidate"]["deps"]
    assert parsed["env"]["release-reference"]["deps"][0].endswith("requirements.release.txt")
    assert parsed["env"]["release-candidate"]["constrain_package_deps"] is True
    assert parsed["env"]["release-candidate"]["use_frozen_constraints"] is True
    assert parsed["env"]["release-reference"]["pass_env"] == []
    assert "git" not in rendered


@pytest.mark.skipif(
    os.environ.get("PARITY_WORKSPACE_FLOOR_SMOKE") != "1",
    reason="requires the exact managed-workspace tool floor installed by CI",
)
def test_generated_tox_config_runs_on_declared_tool_floor(tmp_path: Path) -> None:
    expected_versions = {
        "tox": "4.44.0",
        "tox-uv": "1.29.0",
        "uv": "0.9.1",
    }
    assert {
        distribution: distribution_version(distribution) for distribution in expected_versions
    } == expected_versions

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    state_root = project / ".parity" / "workspace"
    locks = state_root / "locks"
    locks.mkdir(parents=True)
    (locks / "requirements.default.txt").write_text(
        "# Deliberately empty: this smoke test exercises tox without resolving packages.\n",
        encoding="utf-8",
    )
    workspace = ResolvedWorkspace(
        path=project / "parity.workspace.toml",
        root=project,
        reference="candidate-lib==1.9.0",
        reference_extras=(),
        candidate=project,
        candidate_package="editable",
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
        config=project / "parity.toml",
        manifest=project / "migration.toml",
        report_dir=state_root / "reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    tox_config = state_root / "tox.toml"
    tox_config.write_text(
        render_tox_config(workspace, state_root=state_root),
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tox",
            "--colored",
            "no",
            "-c",
            str(tox_config),
            "--root",
            str(project),
            "--workdir",
            str(state_root / "envs"),
            "run",
            "--notest",
            "-e",
            "default-reference",
        ],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (state_root / "envs" / "default-reference").is_dir()


def test_setup_compiles_locks_runs_tox_and_queries_worker_interpreters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release.in"
    current = tmp_path / "current.in"
    release.write_text("pandas==2.2.3\n", encoding="utf-8")
    current.write_text("pandas>=2.2\n", encoding="utf-8")
    workspace_path = _project(
        tmp_path,
        lanes=(
            WorkspaceLane(name="release", requirements=Path("release.in")),
            WorkspaceLane(name="current", requirements=Path("current.in")),
        ),
    )
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace.shutil.which", tools.get)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        assert "shell" not in kwargs
        if command[0] == "/tools/uv":
            output = Path(command[command.index("--output-file") + 1])
            output.write_text(
                f"parity-check=={__version__} \\\n"
                "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if "config" in command:
            env_name = command[command.index("-e") + 1]
            python = _executable(tmp_path / "fake-envs" / env_name / "bin" / "python")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"[testenv:{env_name}]\nenv_python = {python}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("parity.migration_workspace.subprocess.run", fake_run)

    prepared = setup_workspace(workspace_path)

    assert [lane.name for lane in prepared.lanes] == ["release", "current"]
    assert len([command for command, _ in calls if command[0] == "/tools/uv"]) == 2
    assert all(
        "--generate-hashes" in command and "--no-header" in command and "--no-annotate" in command
        for command, _ in calls
        if command[0] == "/tools/uv"
    )
    assert len([command for command, _ in calls if "config" in command]) == 4
    tox_runs = [command for command, _ in calls if command[-2:] == ["run", "--notest"]]
    assert len(tox_runs) == 1
    assert all(kwargs["capture_output"] is True for _, kwargs in calls)
    assert all(kwargs["check"] is False for _, kwargs in calls)
    generated = (tmp_path / ".parity/workspace/inputs/release.in").read_text(encoding="utf-8")
    assert generated == (
        f"# Generated by Parity. Do not edit.\nparity-check=={__version__}\ncandidate-lib==1.9.0\n"
    )
    assert prepared.tox_config == tmp_path / ".parity/workspace/tox.toml"


def test_setup_missing_optional_tool_is_actionable(tmp_path: Path, monkeypatch) -> None:
    workspace_path = _project(tmp_path)
    monkeypatch.setattr("parity.migration_workspace.shutil.which", lambda _name: None)

    with pytest.raises(WorkspaceError, match=r"parity-check\[workspace\]"):
        setup_workspace(workspace_path)


def test_setup_failure_hides_subprocess_output_and_preserves_previous_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_path = _project(tmp_path)
    lock = tmp_path / ".parity/workspace/locks/requirements.default.txt"
    lock.parent.mkdir(parents=True)
    previous = "parity-check==0.9.2\n"
    lock.write_text(previous, encoding="utf-8")
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace.shutil.which", tools.get)

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="/private/project/source",
            stderr="https://token@example.invalid/simple",
        )

    monkeypatch.setattr("parity.migration_workspace.subprocess.run", fail)

    with pytest.raises(WorkspaceError) as caught:
        setup_workspace(workspace_path)

    assert "/private" not in str(caught.value)
    assert "token" not in str(caught.value)
    assert ".parity/workspace/logs/resolve-default.log" in str(caught.value)
    assert lock.read_text(encoding="utf-8") == previous
    private_log = tmp_path / ".parity/workspace/logs/resolve-default.log"
    assert "/private/project/source" in private_log.read_text(encoding="utf-8")
    assert "token@example.invalid" in private_log.read_text(encoding="utf-8")


def test_private_state_symlink_cannot_escape_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace_path = _project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / ".parity").symlink_to(outside, target_is_directory=True)
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace.shutil.which", tools.get)

    with pytest.raises(WorkspaceError, match=r"inside the workspace project|resolves outside"):
        setup_workspace(workspace_path)


@pytest.mark.parametrize("child", ["inputs", "locks", "envs", "logs"])
def test_private_state_child_symlink_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child: str,
) -> None:
    workspace_path = _project(tmp_path)
    state = tmp_path / ".parity/workspace"
    state.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-{child}-outside"
    outside.mkdir()
    (state / child).symlink_to(outside, target_is_directory=True)
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace.shutil.which", tools.get)

    with pytest.raises(WorkspaceError, match=rf"{child!r} directory cannot be a symlink"):
        setup_workspace(workspace_path)

    assert list(outside.iterdir()) == []


def test_run_workspace_overrides_every_case_worker_without_mutating_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path
    workspace = ResolvedWorkspace(
        path=project / "parity.workspace.toml",
        root=project,
        reference="candidate-lib==1.9.0",
        reference_extras=(),
        candidate=project,
        candidate_package="editable",
        python="3.12",
        config=project / "parity.toml",
        manifest=project / "migration.toml",
        report_dir=project / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("release", None), ResolvedWorkspaceLane("current", None)),
    )
    environments = tuple(
        LaneEnvironment(
            name=name,
            reference_env=f"{name}-reference",
            candidate_env=f"{name}-candidate",
            reference_python=_executable(project / "envs" / name / "ref" / "python"),
            candidate_python=_executable(project / "envs" / name / "cand" / "python"),
        )
        for name in ("release", "current")
    )
    setup = WorkspaceSetup(
        workspace=workspace,
        tox_config=project / ".parity/workspace/tox.toml",
        lanes=environments,
    )
    config = _config()
    manifest = MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])])
    seen: list[tuple[Path | None, Path | None]] = []
    sentinel = cast(MigrationResult, object())
    monkeypatch.setattr(
        "parity.migration_workspace.setup_workspace",
        lambda _path, *, refresh_locks: setup,
    )
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: config)
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest", lambda _path: manifest
    )

    def fake_run(_manifest: MigrationManifest, effective: ParityConfig) -> MigrationResult:
        seen.append((effective.cases[0].reference.python, effective.cases[0].candidate.python))
        return sentinel

    monkeypatch.setattr("parity.migration_workspace.run_migration", fake_run)
    reports: list[Path] = []
    progress: list[tuple[str, str | None]] = []

    def fake_write(_result: MigrationResult, destination: str | Path) -> Path:
        report = Path(destination)
        reports.append(report)
        return report

    monkeypatch.setattr("parity.migration_workspace.write_migration_json", fake_write)

    result = run_workspace(
        project / "parity.workspace.toml",
        refresh_locks=True,
        progress=lambda event, lane: progress.append((event, lane)),
    )

    assert [lane.name for lane in result.lanes] == ["release", "current"]
    assert seen == [
        (environments[0].reference_python, environments[0].candidate_python),
        (environments[1].reference_python, environments[1].candidate_python),
    ]
    assert config.cases[0].reference.python is None
    assert config.cases[0].candidate.python is None
    assert reports == [
        project / ".parity/workspace/reports/release.json",
        project / ".parity/workspace/reports/current.json",
    ]
    assert [lane.report for lane in result.lanes] == reports
    assert progress == [
        ("setup", None),
        ("lane", "release"),
        ("complete", "release"),
        ("lane", "current"),
        ("complete", "current"),
    ]


def test_migration_init_cli_creates_only_declarative_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "parity.workspace.toml"

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference",
            "candidate-lib==1.9.0",
            "--workspace",
            str(workspace),
            "--candidate",
            "candidate",
            "--lane",
            "release=release.in",
            "--lane",
            "current=current.in",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "parity migration run" in result.stdout
    assert "uses existing parity.toml and migration.toml" in result.stdout
    document = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert document["reference"] == "candidate-lib==1.9.0"
    assert [lane["name"] for lane in document["lanes"]] == ["release", "current"]
    assert document["report_dir"] == ".parity/workspace/reports"
    assert not (tmp_path / ".parity").exists()


def test_migration_setup_cli_reports_environment_names_without_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = SimpleNamespace(
        lanes=[
            SimpleNamespace(
                name="default",
                reference_env="default-reference",
                candidate_env="default-candidate",
            )
        ]
    )
    monkeypatch.setattr(
        "parity.migration_workspace.setup_workspace",
        lambda _path, *, refresh_locks: prepared,
    )

    result = runner.invoke(
        cli.app,
        ["migration", "setup", "--workspace", str(tmp_path / "parity.workspace.toml")],
    )

    assert result.exit_code == 0, result.output
    assert "1 dependency lane" in result.stdout
    assert "default-reference, default-candidate" in result.stdout
    assert str(tmp_path) not in result.stdout
