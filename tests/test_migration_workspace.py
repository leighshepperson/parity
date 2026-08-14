from __future__ import annotations

import json
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

import parity.migration_workspace as migration_workspace_module
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
    advance_workspace,
    load_workspace,
    parse_lane_options,
    rebase_workspace_path,
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
from parity.templates import write_starter

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


def _write_resolved_workspace_document(workspace: ResolvedWorkspace) -> None:
    """Materialize the human-authored contract for tests that mock setup."""

    workspace.root.mkdir(parents=True, exist_ok=True)
    write_workspace(
        workspace.path,
        reference=workspace.reference,
        candidate=Path(os.path.relpath(workspace.candidate, workspace.root)),
        python_version=workspace.python,
        config=Path(os.path.relpath(workspace.config, workspace.root)),
        manifest=Path(os.path.relpath(workspace.manifest, workspace.root)),
        report_dir=Path(os.path.relpath(workspace.report_dir, workspace.root)),
        lanes=[
            WorkspaceLane(
                name=lane.name,
                requirements=(
                    Path(os.path.relpath(lane.requirements, workspace.root))
                    if lane.requirements is not None
                    else None
                ),
            )
            for lane in workspace.lanes
        ],
    )


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


def test_workspace_init_rebases_invocation_paths_beside_nested_document(
    tmp_path: Path,
) -> None:
    invocation = tmp_path / "project"
    destination = invocation / "migrations" / "parity.workspace.toml"
    invocation.mkdir()

    written = write_workspace(
        Path("migrations/parity.workspace.toml"),
        reference="candidate-lib==1.9.0",
        candidate=Path("candidate-src/candidate-lib"),
        config=Path("migrations/parity.toml"),
        manifest=Path("migrations/migration.toml"),
        lanes=[
            WorkspaceLane(
                name="minimum",
                requirements=Path("requirements/minimum.in"),
            )
        ],
        invocation_cwd=invocation,
    )

    assert written == destination
    document = tomllib.loads(destination.read_text(encoding="utf-8"))
    assert document["candidate"] == "../candidate-src/candidate-lib"
    assert document["config"] == "parity.toml"
    assert document["manifest"] == "migration.toml"
    assert document["report_dir"] == ".parity/workspace/reports"
    assert document["lanes"][0]["requirements"] == "../requirements/minimum.in"
    assert rebase_workspace_path(
        Path("migrations/parity.toml"),
        workspace_path=Path("migrations/parity.workspace.toml"),
        invocation_cwd=invocation,
    ) == Path("parity.toml")


def test_advance_workspace_preserves_harness_and_locks_but_invalidates_reports(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release.in"
    release.write_text("pandas<3\n", encoding="utf-8")
    workspace_path = _project(
        tmp_path,
        lanes=(WorkspaceLane(name="release", requirements=Path("release.in")),),
    )
    report = tmp_path / ".parity/workspace/reports/release.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"passed"}\n', encoding="utf-8")
    obsolete = report.parent / "removed-lane.json"
    obsolete.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "summary": {},
                "units": [],
                "manifest_sha256": "0" * 64,
                "parity": {},
            }
        ),
        encoding="utf-8",
    )
    unrelated_json = report.parent / "notes.json"
    unrelated_json.write_text('{"project":"consumer"}\n', encoding="utf-8")
    note = report.parent / "README.txt"
    note.write_text("user note\n", encoding="utf-8")
    lock = tmp_path / ".parity/workspace/locks/requirements.release.txt"
    lock.parent.mkdir(parents=True)
    lock.write_text("candidate-lib==1.9.0\n", encoding="utf-8")

    advanced = advance_workspace(workspace_path, reference="candidate-lib==2.0.0")

    assert advanced == workspace_path
    document = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    assert document["reference"] == "candidate-lib==2.0.0"
    assert document["candidate"] == "."
    assert document["lanes"] == [{"name": "release", "requirements": "release.in"}]
    assert not report.exists()
    assert not obsolete.exists()
    assert unrelated_json.read_text(encoding="utf-8") == '{"project":"consumer"}\n'
    assert note.read_text(encoding="utf-8") == "user note\n"
    assert lock.read_text(encoding="utf-8") == "candidate-lib==1.9.0\n"

    for non_advance in ("candidate-lib==2.0.0", "candidate-lib==1.8.0"):
        with pytest.raises(WorkspaceError, match="must be newer"):
            advance_workspace(workspace_path, reference=non_advance)
    with pytest.raises(WorkspaceError, match="cannot change the subject distribution"):
        advance_workspace(workspace_path, reference="other-lib==3.0.0")
    assert tomllib.loads(workspace_path.read_text(encoding="utf-8"))["reference"] == (
        "candidate-lib==2.0.0"
    )


def test_workspace_requires_dedicated_report_subdirectory(tmp_path: Path) -> None:
    workspace = tmp_path / "parity.workspace.toml"

    with pytest.raises(WorkspaceError, match="dedicated contained subdirectory"):
        write_workspace(
            workspace,
            reference="candidate-lib==1.9.0",
            report_dir=Path("."),
        )

    assert not workspace.exists()


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
    assert (tmp_path / ".parity/.gitignore").read_text(encoding="utf-8") == "*\n"
    assert not (tmp_path / ".gitignore").exists()


def test_setup_missing_optional_tool_is_actionable(tmp_path: Path, monkeypatch) -> None:
    workspace_path = _project(tmp_path)
    monkeypatch.setattr("parity.migration_workspace.shutil.which", lambda _name: None)

    with pytest.raises(WorkspaceError, match=r"parity-check\[workspace\]"):
        setup_workspace(workspace_path)


def test_run_invalidates_active_report_before_environment_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    harness = tmp_path / "migrations"
    harness.mkdir()
    (harness / "parity.toml").write_text("", encoding="utf-8")
    (harness / "migration.toml").write_text("", encoding="utf-8")
    workspace_path = harness / "parity.workspace.toml"
    write_workspace(
        workspace_path,
        reference="candidate-lib==1.9.0",
        candidate=Path(".."),
    )
    report = harness / ".parity/workspace/reports/default.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"passed"}\n', encoding="utf-8")

    def fail_setup(_workspace: ResolvedWorkspace, *, refresh_locks: bool) -> WorkspaceSetup:
        raise WorkspaceError("controlled setup failure")

    monkeypatch.setattr("parity.migration_workspace._setup_resolved_workspace", fail_setup)
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: _config())
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])]),
    )

    with pytest.raises(WorkspaceError, match="controlled setup failure"):
        run_workspace(workspace_path)

    assert not report.exists()


def test_private_state_preserves_existing_gitignore_policy(tmp_path: Path) -> None:
    workspace = load_workspace(_project(tmp_path))
    ignore = tmp_path / ".parity/.gitignore"
    ignore.parent.mkdir()
    custom = "# consumer policy\nworkspace/envs/\n"
    ignore.write_text(custom, encoding="utf-8")

    state = migration_workspace_module._state_root(workspace)

    assert state == tmp_path / ".parity/workspace"
    assert ignore.read_text(encoding="utf-8") == custom
    assert not (tmp_path / ".gitignore").exists()


def test_private_state_rejects_redirect_before_creating_outside_child(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = load_workspace(_project(project))
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".parity").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="root cannot be a symbolic link"):
        migration_workspace_module._state_root(workspace)

    assert not (outside / "workspace").exists()
    assert list(outside.iterdir()) == []


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
    # Keep reports outside .parity so setup reaches the private-state guard itself.
    write_workspace(
        workspace_path,
        reference="candidate-lib==1.9.0",
        candidate=Path("."),
        python_version="3.12",
        report_dir=Path("reports"),
        force=True,
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / ".parity").symlink_to(outside, target_is_directory=True)
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace.shutil.which", tools.get)

    with pytest.raises(WorkspaceError, match="root cannot be a symbolic link"):
        setup_workspace(workspace_path)

    assert not (outside / "workspace").exists()


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
        reference="Candidate_Lib==1.9.0",
        reference_extras=(),
        candidate=project.parent / f"{project.name}-candidate-lib",
        candidate_package="editable",
        python="3.12",
        config=project / "parity.toml",
        manifest=project / "migration.toml",
        report_dir=project / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("release", None), ResolvedWorkspaceLane("current", None)),
    )
    _write_resolved_workspace_document(workspace)
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
    seen: list[tuple[Path | None, Path | None, dict[str, str], list[str], list[str]]] = []
    sentinel = cast(MigrationResult, object())
    monkeypatch.setattr("parity.migration_workspace.load_workspace", lambda _path: workspace)
    monkeypatch.setattr(
        "parity.migration_workspace._setup_resolved_workspace",
        lambda _workspace, *, refresh_locks: setup,
    )
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: config)
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest", lambda _path: manifest
    )

    def fake_run(_manifest: MigrationManifest, effective: ParityConfig) -> MigrationResult:
        active_lane = ("release", "current")[len(seen)]
        assert not (project / f".parity/workspace/reports/{active_lane}.json").exists()
        case = effective.cases[0]
        seen.append(
            (
                case.reference.python,
                case.candidate.python,
                case.reference.required_distributions,
                case.reference.record_distributions,
                case.candidate.record_distributions,
            )
        )
        assert case.reference.workdir == project
        assert case.candidate.workdir == project
        return sentinel

    monkeypatch.setattr("parity.migration_workspace.run_migration", fake_run)
    reports: list[Path] = []
    progress: list[tuple[str, str | None]] = []

    def fake_write(_result: MigrationResult, destination: str | Path) -> Path:
        report = Path(destination)
        reports.append(report)
        return report

    monkeypatch.setattr("parity.migration_workspace.write_migration_json", fake_write)
    report_root = project / ".parity/workspace/reports"
    report_root.mkdir(parents=True)
    for lane in ("release", "current"):
        (report_root / f"{lane}.json").write_text("stale\n", encoding="utf-8")

    result = run_workspace(
        project / "parity.workspace.toml",
        refresh_locks=True,
        progress=lambda event, lane: progress.append((event, lane)),
    )

    assert [lane.name for lane in result.lanes] == ["release", "current"]
    assert seen == [
        (
            environments[0].reference_python,
            environments[0].candidate_python,
            {"candidate-lib": "==1.9.0"},
            ["candidate-lib"],
            ["candidate-lib"],
        ),
        (
            environments[1].reference_python,
            environments[1].candidate_python,
            {"candidate-lib": "==1.9.0"},
            ["candidate-lib"],
            ["candidate-lib"],
        ),
    ]
    assert config.cases[0].reference.python is None
    assert config.cases[0].candidate.python is None
    assert config.cases[0].reference.required_distributions == {}
    assert config.cases[0].reference.record_distributions == []
    assert config.cases[0].candidate.record_distributions == []
    assert config.cases[0].reference.workdir is None
    assert config.cases[0].candidate.workdir is None
    assert reports == [
        project / ".parity/workspace/reports/release.json",
        project / ".parity/workspace/reports/current.json",
    ]
    assert [lane.report for lane in result.lanes] == reports
    assert not (report_root / "release.json").exists()
    assert not (report_root / "current.json").exists()
    assert progress == [
        ("setup", None),
        ("lane", "release"),
        ("complete", "release"),
        ("lane", "current"),
        ("complete", "current"),
    ]


def test_run_workspace_rejects_conflicting_subject_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = ResolvedWorkspace(
        path=tmp_path / "parity.workspace.toml",
        root=tmp_path,
        reference="candidate-lib==1.9.0",
        reference_extras=(),
        candidate=tmp_path / "candidate-src" / "candidate-lib",
        candidate_package="editable",
        python="3.12",
        config=tmp_path / "parity.toml",
        manifest=tmp_path / "migration.toml",
        report_dir=tmp_path / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    _write_resolved_workspace_document(workspace)
    config = _config()
    config.cases[0].reference.required_distributions = {"candidate-lib": "==1.8.0"}
    monkeypatch.setattr("parity.migration_workspace.load_workspace", lambda _path: workspace)
    monkeypatch.setattr(
        "parity.migration_workspace._setup_resolved_workspace",
        lambda _workspace, *, refresh_locks: pytest.fail("setup must not run"),
    )
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: config)
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])]),
    )

    with pytest.raises(WorkspaceError, match="conflict with the workspace subject"):
        run_workspace(tmp_path / "parity.workspace.toml")

    assert config.cases[0].reference.required_distributions == {"candidate-lib": "==1.8.0"}


@pytest.mark.parametrize(
    "exposure",
    [
        "workspace-candidate",
        "workspace-ancestor",
        "workspace-src",
        "pythonpath-candidate",
        "pythonpath-ancestor",
        "pythonpath-src",
    ],
)
def test_run_workspace_rejects_candidate_source_visible_to_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exposure: str,
) -> None:
    candidate = tmp_path / "candidate"
    (candidate / "candidate_lib").mkdir(parents=True)
    (candidate / "candidate_lib/__init__.py").write_text("", encoding="utf-8")
    unsafe_roots = {
        "candidate": candidate,
        "ancestor": candidate.parent,
        "src": candidate / "src",
    }
    kind, location = exposure.split("-", 1)
    root = unsafe_roots[location] if kind == "workspace" else candidate / "migrations"
    workspace = ResolvedWorkspace(
        path=root / "parity.workspace.toml",
        root=root,
        reference="candidate-lib==1.9.0",
        reference_extras=(),
        candidate=candidate,
        candidate_package="editable",
        python="3.12",
        config=root / "parity.toml",
        manifest=root / "migration.toml",
        report_dir=root / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    _write_resolved_workspace_document(workspace)
    setup = WorkspaceSetup(
        workspace=workspace,
        tox_config=root / ".parity/workspace/tox.toml",
        lanes=(),
    )
    monkeypatch.setattr("parity.migration_workspace.load_workspace", lambda _path: workspace)
    setup_invocations: list[ResolvedWorkspace] = []

    def record_setup(resolved: ResolvedWorkspace, *, refresh_locks: bool) -> WorkspaceSetup:
        setup_invocations.append(resolved)
        return setup

    monkeypatch.setattr(
        "parity.migration_workspace._setup_resolved_workspace",
        record_setup,
    )
    monkeypatch.setattr(
        "parity.migration_workspace.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: _config())
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])]),
    )
    if kind == "pythonpath":
        monkeypatch.setenv("PYTHONPATH", str(unsafe_roots[location]))
    else:
        monkeypatch.delenv("PYTHONPATH", raising=False)

    with pytest.raises(WorkspaceError, match=r"workspace root exposes|PYTHONPATH exposes"):
        run_workspace(root / "parity.workspace.toml")

    assert setup_invocations == []


def test_run_workspace_allows_harness_subdirectory_inside_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    root = candidate / "migrations"
    workspace = ResolvedWorkspace(
        path=root / "parity.workspace.toml",
        root=root,
        reference="candidate-lib==1.9.0",
        reference_extras=(),
        candidate=candidate,
        candidate_package="editable",
        python="3.12",
        config=root / "parity.toml",
        manifest=root / "migration.toml",
        report_dir=root / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    _write_resolved_workspace_document(workspace)
    reference_python = _executable(tmp_path / "envs/reference/python")
    candidate_python = _executable(tmp_path / "envs/candidate/python")
    setup = WorkspaceSetup(
        workspace=workspace,
        tox_config=root / ".parity/workspace/tox.toml",
        lanes=(
            LaneEnvironment(
                name="default",
                reference_env="default-reference",
                candidate_env="default-candidate",
                reference_python=reference_python,
                candidate_python=candidate_python,
            ),
        ),
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr("parity.migration_workspace.load_workspace", lambda _path: workspace)
    monkeypatch.setattr(
        "parity.migration_workspace._setup_resolved_workspace",
        lambda _workspace, *, refresh_locks: setup,
    )
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: _config())
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest",
        lambda _path: MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])]),
    )
    seen: list[tuple[Path | None, Path | None, Path | None, Path | None]] = []
    sentinel = cast(MigrationResult, object())

    def fake_run(_manifest: MigrationManifest, config: ParityConfig) -> MigrationResult:
        case = config.cases[0]
        seen.append(
            (
                case.reference.workdir,
                case.candidate.workdir,
                case.reference.python,
                case.candidate.python,
            )
        )
        return sentinel

    monkeypatch.setattr("parity.migration_workspace.run_migration", fake_run)
    monkeypatch.setattr(
        "parity.migration_workspace.write_migration_json",
        lambda _result, destination: Path(destination),
    )

    result = run_workspace(root / "parity.workspace.toml")

    assert [lane.name for lane in result.lanes] == ["default"]
    assert seen == [(root, root, reference_python, candidate_python)]


def test_migration_init_cli_creates_only_declarative_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "parity.workspace.toml"
    write_starter(tmp_path / "parity.toml")
    monkeypatch.chdir(tmp_path)

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
            "--config",
            "parity.toml",
            "--manifest",
            "migration.toml",
            "--lane",
            "release=release.in",
            "--lane",
            "current=current.in",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "parity migration run" in result.stdout
    assert "active pair declared" in result.stdout
    assert "created starter ledger" in result.stdout
    document = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert document["reference"] == "candidate-lib==1.9.0"
    assert [lane["name"] for lane in document["lanes"]] == ["release", "current"]
    assert document["report_dir"] == ".parity/workspace/reports"
    manifest = tomllib.loads((tmp_path / "migration.toml").read_text(encoding="utf-8"))
    assert manifest["units"][0]["id"] == "core-regression"
    assert not (tmp_path / ".parity").exists()


def test_default_cli_flow_creates_nested_active_pair_and_advances_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    write_starter(tmp_path / "migrations/parity.toml")
    monkeypatch.chdir(tmp_path)

    initialized = runner.invoke(
        cli.app,
        ["migration", "init", "--reference", "candidate-lib==1.9.0"],
    )

    assert initialized.exit_code == 0, initialized.output
    workspace = tmp_path / "migrations/parity.workspace.toml"
    document = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert document["candidate"] == ".."
    assert document["config"] == "parity.toml"
    assert document["manifest"] == "migration.toml"
    assert (tmp_path / "migrations/migration.toml").is_file()

    report = tmp_path / "migrations/.parity/workspace/reports/default.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"passed"}\n', encoding="utf-8")
    advanced = runner.invoke(
        cli.app,
        ["migration", "advance", "--reference", "candidate-lib==2.0.0"],
    )

    assert advanced.exit_code == 0, advanced.output
    assert "previous active lane reports were invalidated" in advanced.stdout
    assert not report.exists()
    assert tomllib.loads(workspace.read_text(encoding="utf-8"))["reference"] == (
        "candidate-lib==2.0.0"
    )


def test_nested_cli_workspace_rejects_report_directory_outside_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_starter(tmp_path / "migrations/parity.toml")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference",
            "candidate-lib==1.9.0",
            "--report-dir",
            "reports",
        ],
    )

    assert result.exit_code == 2
    assert "must stay inside the workspace directory" in result.stderr
    assert not (tmp_path / "migrations/parity.workspace.toml").exists()
    assert not (tmp_path / "migrations/migration.toml").exists()


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
