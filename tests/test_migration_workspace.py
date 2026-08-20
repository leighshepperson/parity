from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pyarrow as pa
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import parity.migration_workspace as migration_workspace_module
from parity import cli
from parity.agent_output import ChecklistItemId, ContractChecklist
from parity.engine import replay_artifact
from parity.migration import (
    MigrationManifest,
    MigrationResult,
    MigrationUnit,
    migration_report_payload,
    run_migration,
)
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
    GenerationConfig,
    ParityConfig,
    PerformanceConfig,
    Status,
)
from parity.templates import write_starter

runner = CliRunner()
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _normalized_cli_stderr(stderr: str) -> str:
    return " ".join(_ANSI_ESCAPE.sub("", stderr).split())


def _project(tmp_path: Path, *, lanes: tuple[WorkspaceLane, ...] = ()) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    write_starter(tmp_path / "parity.toml")
    (tmp_path / "migration.toml").write_text("", encoding="utf-8")
    workspace = tmp_path / "parity.workspace.toml"
    write_workspace(
        workspace,
        reference_package="candidate-lib==1.9.0",
        candidate_package="candidate-lib==2.0.0",
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
        reference_package=workspace.reference_package,
        reference_path=(
            Path(os.path.relpath(workspace.reference_path, workspace.root))
            if workspace.reference_path is not None
            else None
        ),
        candidate_package=workspace.candidate_package,
        candidate_path=(
            Path(os.path.relpath(workspace.candidate_path, workspace.root))
            if workspace.candidate_path is not None
            else None
        ),
        python_version=(
            workspace.reference_python
            if workspace.reference_python == workspace.candidate_python
            else None
        ),
        reference_python_version=(
            workspace.reference_python
            if workspace.reference_python != workspace.candidate_python
            else None
        ),
        candidate_python_version=(
            workspace.candidate_python
            if workspace.reference_python != workspace.candidate_python
            else None
        ),
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


def _git_checkout(path: Path, *, version: str, value: str) -> Path:
    """Create one tiny committed package checkout for source-integrity tests."""

    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "candidate-lib"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    package = path / "candidate_lib"
    package.mkdir()
    (package / "__init__.py").write_text(f'VALUE = "{value}"\n', encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "parity-tests@example.invalid"],
        ["git", "config", "user.name", "Parity Tests"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "initial"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    return path


def test_workspace_model_rejects_ambiguous_inputs_and_parses_lanes() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        MigrationWorkspace(python="3.12")
    with pytest.raises(ValidationError, match="exactly one"):
        MigrationWorkspace(
            reference_package="candidate-lib==1",
            reference_path=Path("../reference"),
            candidate_path=Path("."),
            python="3.12",
        )
    with pytest.raises(ValidationError, match="exact requirement"):
        MigrationWorkspace(
            reference_package="candidate-lib>=1",
            candidate_path=Path("."),
            python="3.12",
        )
    for invalid in ("candidate-lib==banana", "candidate-lib==1..2", "candidate-lib[-]==1"):
        with pytest.raises(ValidationError, match="exact requirement"):
            MigrationWorkspace(
                reference_package=invalid,
                candidate_path=Path("."),
                python="3.12",
            )
    with pytest.raises(ValidationError, match=r"at least 3\.8"):
        MigrationWorkspace(
            reference_package="candidate-lib==1",
            candidate_path=Path("."),
            python="3.7",
        )
    with pytest.raises(ValidationError, match="both reference_python and candidate_python"):
        MigrationWorkspace(
            reference_package="candidate-lib==1",
            candidate_path=Path("."),
            reference_python="3.8",
        )
    with pytest.raises(ValidationError, match="lane names must be unique"):
        MigrationWorkspace(
            reference_package="candidate-lib==1",
            candidate_path=Path("."),
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

    split = MigrationWorkspace(
        reference_package="candidate-lib==1",
        candidate_path=Path("."),
        reference_python="3.8",
        candidate_python="3.13",
    )
    assert split.effective_reference_python == "3.8"
    assert split.effective_candidate_python == "3.13"

    released_pair = MigrationWorkspace(
        reference_package="candidate-lib[io]==1",
        candidate_package="candidate-lib[io]==2",
        python="3.12",
    )
    assert released_pair.reference_extras == ("io",)
    assert released_pair.candidate_extras == ("io",)
    assert released_pair.candidate_path is None
    with pytest.raises(ValidationError, match="exactly one"):
        MigrationWorkspace(
            reference_package="candidate-lib==1",
            candidate_package="candidate-lib==2",
            candidate_path=Path("candidate"),
            python="3.12",
        )
    with pytest.raises(ValidationError, match="exact requirement"):
        MigrationWorkspace(
            reference_package="candidate-lib==1",
            candidate_package="candidate-lib>=2",
            python="3.12",
        )


def test_workspace_model_rejects_v1_and_legacy_source_keys() -> None:
    with pytest.raises(ValidationError):
        MigrationWorkspace.model_validate(
            {
                "version": 1,
                "reference_package": "candidate-lib==1",
                "candidate_path": ".",
                "python": "3.12",
            }
        )

    with pytest.raises(ValidationError):
        MigrationWorkspace.model_validate(
            {
                "version": 2,
                "reference": "candidate-lib==1",
                "candidate": ".",
                "python": "3.12",
            }
        )


def test_released_workspace_resolves_both_exact_package_sources(tmp_path: Path) -> None:
    (tmp_path / "parity.toml").write_text("configured\n", encoding="utf-8")
    (tmp_path / "migration.toml").write_text("declared\n", encoding="utf-8")
    workspace_path = write_workspace(
        tmp_path / "parity.workspace.toml",
        reference_package="Candidate_Lib[io]==1.9.0",
        candidate_package="candidate-lib[io]==2.0.0",
        python_version="3.12",
    )

    document = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    resolved = load_workspace(workspace_path)

    assert document["version"] == 3
    assert document["reference_package"] == "Candidate_Lib[io]==1.9.0"
    assert document["candidate_package"] == "candidate-lib[io]==2.0.0"
    assert "candidate_path" not in document
    assert resolved.reference_package == "Candidate_Lib[io]==1.9.0"
    assert resolved.candidate_package == "candidate-lib[io]==2.0.0"
    assert resolved.candidate_path is None
    assert resolved.reference_extras == ("io",)
    assert resolved.candidate_extras == ("io",)
    assert resolved.subject_name == "candidate-lib"
    assert not resolved.has_local_sources


def test_local_reference_can_compare_with_released_candidate(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "1.9.0"\n',
        encoding="utf-8",
    )
    harness = tmp_path / "migrations"
    harness.mkdir()
    (harness / "parity.toml").write_text("configured\n", encoding="utf-8")
    (harness / "migration.toml").write_text("declared\n", encoding="utf-8")
    workspace_path = write_workspace(
        harness / "parity.workspace.toml",
        reference_path=Path("../reference"),
        candidate_package="candidate-lib==2.0.0",
        python_version="3.12",
    )

    resolved = load_workspace(workspace_path)

    assert resolved.reference_package is None
    assert resolved.reference_path == reference.resolve()
    assert resolved.reference_install_mode == "editable"
    assert resolved.candidate_path is None
    assert resolved.candidate_package == "candidate-lib==2.0.0"
    assert resolved.has_local_sources
    assert not resolved.is_local_comparison


def test_local_workspace_resolves_distinct_matching_checkouts(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    candidate = tmp_path / "candidate"
    harness = tmp_path / "harness"
    for source, version in ((reference, "1.0.0"), (candidate, "2.0.0")):
        source.mkdir()
        (source / "pyproject.toml").write_text(
            f'[project]\nname = "Candidate_Lib"\nversion = "{version}"\n',
            encoding="utf-8",
        )
    harness.mkdir()
    (harness / "parity.toml").write_text("configured\n", encoding="utf-8")
    (harness / "migration.toml").write_text("declared\n", encoding="utf-8")

    workspace_path = write_workspace(
        harness / "parity.workspace.toml",
        reference_path=Path("../reference"),
        candidate_path=Path("../candidate"),
        reference_python_version="3.8",
        candidate_python_version="3.12",
    )
    document = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    resolved = load_workspace(workspace_path)

    assert document["version"] == 3
    assert "reference_package" not in document
    assert document["reference_path"] == "../reference"
    assert document["candidate_path"] == "../candidate"
    assert "python" not in document
    assert document["reference_python"] == "3.8"
    assert document["candidate_python"] == "3.12"
    assert resolved.reference_package is None
    assert resolved.reference_path == reference.resolve()
    assert resolved.reference_install_mode == "editable"
    assert resolved.candidate_path == candidate.resolve()
    assert resolved.candidate_install_mode == "editable"
    assert resolved.reference_python == "3.8"
    assert resolved.candidate_python == "3.12"
    assert resolved.subject_name == "candidate-lib"
    assert resolved.is_local_comparison

    (candidate / "pyproject.toml").write_text(
        '[project]\nname = "different-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError, match="does not match reference distribution"):
        load_workspace(workspace_path)


def test_local_workspace_rejects_same_checkout_and_dynamic_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "setup.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    (tmp_path / "parity.toml").write_text("configured\n", encoding="utf-8")
    (tmp_path / "migration.toml").write_text("declared\n", encoding="utf-8")
    workspace_path = write_workspace(
        tmp_path / "parity.workspace.toml",
        reference_path=Path("source"),
        candidate_path=Path("source"),
        python_version="3.12",
    )

    with pytest.raises(WorkspaceError, match="not declared statically"):
        load_workspace(workspace_path)

    (source / "setup.cfg").write_text(
        "[metadata]\nname = candidate-lib\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError, match="must be different checkouts"):
        load_workspace(workspace_path)


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
    (project / "parity.toml").write_text("", encoding="utf-8")
    (checks / "migration.toml").write_text("", encoding="utf-8")
    workspace_path = project / "parity.workspace.toml"
    write_workspace(
        workspace_path,
        reference_package="candidate-lib[plot,io]==1.9.0",
        candidate_path=Path("candidate"),
        python_version="3.13",
        config=Path("parity.toml"),
        manifest=Path("checks/migration.toml"),
        lanes=[WorkspaceLane(name="current", requirements=Path("requirements-current.in"))],
    )

    (candidate / "pyproject.toml").write_text(
        '[project]\nname = "candidate-lib"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    workspace = load_workspace(workspace_path)

    assert workspace.candidate_path == candidate.resolve()
    assert workspace.config == (project / "parity.toml").resolve()
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
        reference_package="candidate-lib==1.9.0",
        candidate_path=Path("candidate-src/candidate-lib"),
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
    assert document["version"] == 3
    assert document["candidate_path"] == "../candidate-src/candidate-lib"
    assert document["config"] == "parity.toml"
    assert document["manifest"] == "migration.toml"
    assert document["report_dir"] == ".parity/workspace/reports"
    assert document["lanes"][0]["requirements"] == "../requirements/minimum.in"
    assert rebase_workspace_path(
        Path("migrations/parity.toml"),
        workspace_path=Path("migrations/parity.workspace.toml"),
        invocation_cwd=invocation,
    ) == Path("parity.toml")


def test_workspace_rejects_config_directory_outside_managed_runtime_root(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "migrations" / "parity.workspace.toml"

    with pytest.raises(WorkspaceError, match="config must contain the workspace directory"):
        write_workspace(
            workspace_path,
            reference_package="candidate-lib==1.9.0",
            candidate_package="candidate-lib==2.0.0",
            config=Path("../configs/parity.toml"),
        )

    assert not workspace_path.exists()


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

    advanced = advance_workspace(
        workspace_path,
        reference_package="candidate-lib==2.0.0",
    )

    assert advanced == workspace_path
    document = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    assert document["version"] == 3
    assert document["reference_package"] == "candidate-lib==2.0.0"
    assert document["candidate_package"] == "candidate-lib==2.0.0"
    assert document["lanes"] == [{"name": "release", "requirements": "release.in"}]
    assert not report.exists()
    assert not obsolete.exists()
    assert unrelated_json.read_text(encoding="utf-8") == '{"project":"consumer"}\n'
    assert note.read_text(encoding="utf-8") == "user note\n"
    assert lock.read_text(encoding="utf-8") == "candidate-lib==1.9.0\n"

    for non_advance in ("candidate-lib==2.0.0", "candidate-lib==1.8.0"):
        with pytest.raises(WorkspaceError, match="must be newer"):
            advance_workspace(workspace_path, reference_package=non_advance)
    with pytest.raises(WorkspaceError, match="cannot change the subject distribution"):
        advance_workspace(workspace_path, reference_package="other-lib==3.0.0")
    assert (
        tomllib.loads(workspace_path.read_text(encoding="utf-8"))["reference_package"]
        == "candidate-lib==2.0.0"
    )


def test_workspace_requires_dedicated_report_subdirectory(tmp_path: Path) -> None:
    workspace = tmp_path / "parity.workspace.toml"

    with pytest.raises(WorkspaceError, match="dedicated contained subdirectory"):
        write_workspace(
            workspace,
            reference_package="candidate-lib==1.9.0",
            report_dir=Path("."),
        )

    assert not workspace.exists()


def test_workspace_rejects_candidate_with_wrong_or_unverifiable_distribution_name(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "pyproject.toml").write_text(
        '[project]\nname = "other-candidate"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    harness = tmp_path / "migrations"
    write_starter(harness / "parity.toml")
    (harness / "migration.toml").write_text("", encoding="utf-8")
    workspace_path = write_workspace(
        harness / "parity.workspace.toml",
        reference_package="candidate-lib==1.9.0",
        candidate_path=Path("../candidate"),
        python_version="3.12",
    )

    with pytest.raises(WorkspaceError, match="does not match reference distribution"):
        load_workspace(workspace_path)

    (candidate / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (candidate / "setup.py").write_text("# dynamic legacy metadata\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="is not declared statically"):
        load_workspace(workspace_path)


def test_generated_tox_config_pairs_every_lane_with_side_specific_locks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    workspace = ResolvedWorkspace(
        path=project / "parity.workspace.toml",
        root=project,
        reference_package="candidate-lib[io]==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=("io",),
        candidate_package=None,
        candidate_path=project,
        candidate_install_mode="editable",
        candidate_extras=("io",),
        reference_python="3.8",
        candidate_python="3.12",
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
    assert parsed["env"]["release-reference"]["base_python"] == ["3.8"]
    assert parsed["env"]["release-candidate"]["base_python"] == ["3.12"]
    assert parsed["env"]["release-candidate"]["extras"] == ["io"]
    assert parsed["env"]["release-reference"]["deps"][0].endswith(
        "requirements.release.reference.txt"
    )
    assert parsed["env"]["release-candidate"]["deps"][0].endswith(
        "requirements.release.candidate.txt"
    )
    assert parsed["env"]["release-candidate"]["constrain_package_deps"] is True
    assert parsed["env"]["release-candidate"]["use_frozen_constraints"] is True
    assert parsed["env"]["release-reference"]["pass_env"] == []
    assert "git" not in rendered


def test_generated_tox_config_installs_each_local_checkout_in_its_own_worker(
    tmp_path: Path,
) -> None:
    workspace = ResolvedWorkspace(
        path=tmp_path / "harness/parity.workspace.toml",
        root=tmp_path / "harness",
        reference_package=None,
        reference_path=tmp_path / "reference",
        reference_install_mode="editable-legacy",
        reference_extras=(),
        candidate_package=None,
        candidate_path=tmp_path / "candidate",
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.8",
        candidate_python="3.12",
        config=tmp_path / "harness/parity.toml",
        manifest=tmp_path / "harness/migration.toml",
        report_dir=tmp_path / "harness/.parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )

    parsed = tomllib.loads(
        render_tox_config(workspace, state_root=tmp_path / "harness/.parity/workspace")
    )
    reference = parsed["env"]["default-reference"]
    candidate = parsed["env"]["default-candidate"]

    assert reference["package"] == "editable-legacy"
    assert reference["package_root"] == str(tmp_path / "reference")
    assert reference["uv_seed"] is True
    assert reference["constrain_package_deps"] is True
    assert reference["deps"][0].endswith("requirements.default.reference.txt")
    assert candidate["package_root"] == str(tmp_path / "candidate")
    assert candidate["deps"][0].endswith("requirements.default.candidate.txt")
    assert reference["deps"] != candidate["deps"]


def test_generated_tox_config_skips_project_packaging_for_released_pair(
    tmp_path: Path,
) -> None:
    workspace = ResolvedWorkspace(
        path=tmp_path / "migrations/parity.workspace.toml",
        root=tmp_path / "migrations",
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package="candidate-lib==2.0.0",
        candidate_path=None,
        candidate_install_mode=None,
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
        config=tmp_path / "migrations/parity.toml",
        manifest=tmp_path / "migrations/migration.toml",
        report_dir=tmp_path / "migrations/.parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )

    parsed = tomllib.loads(
        render_tox_config(workspace, state_root=tmp_path / "migrations/.parity/workspace")
    )
    reference = parsed["env"]["default-reference"]
    candidate = parsed["env"]["default-candidate"]

    assert reference["package"] == "skip"
    assert candidate["package"] == "skip"
    assert "package_root" not in reference
    assert "package_root" not in candidate
    assert "constrain_package_deps" not in reference
    assert "constrain_package_deps" not in candidate


def test_local_lock_resolution_uses_each_sources_own_dependency_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference"
    candidate = tmp_path / "candidate"
    for source in (reference, candidate):
        source.mkdir()
        (source / "pyproject.toml").write_text(
            '[project]\nname = "candidate-lib"\nversion = "1"\n',
            encoding="utf-8",
        )
    state = tmp_path / "harness/.parity/workspace"
    state.mkdir(parents=True)
    workspace = ResolvedWorkspace(
        path=tmp_path / "harness/parity.workspace.toml",
        root=tmp_path / "harness",
        reference_package=None,
        reference_path=reference,
        reference_install_mode="editable",
        reference_extras=(),
        candidate_package=None,
        candidate_path=candidate,
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.8",
        candidate_python="3.12",
        config=tmp_path / "harness/parity.toml",
        manifest=tmp_path / "harness/migration.toml",
        report_dir=tmp_path / "harness/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        operation: str,
        failure_log: Path,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, operation, failure_log
        commands.append(command)
        output = Path(command[command.index("--output-file") + 1])
        output.write_text("pyarrow==20.0.0\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("parity.migration_workspace._run_checked", fake_run)
    for side in ("reference", "candidate"):
        migration_workspace_module._compile_lane_lock(
            workspace,
            workspace.lanes[0],
            side=side,
            uv="/tools/uv",
            state_root=state,
            refresh=False,
        )

    assert str(reference / "pyproject.toml") in commands[0]
    assert str(candidate / "pyproject.toml") not in commands[0]
    assert commands[0][commands[0].index("--python-version") + 1] == "3.8"
    assert str(candidate / "pyproject.toml") in commands[1]
    assert str(reference / "pyproject.toml") not in commands[1]
    assert commands[1][commands[1].index("--python-version") + 1] == "3.12"
    assert (state / "locks/requirements.default.reference.txt").is_file()
    assert (state / "locks/requirements.default.candidate.txt").is_file()


def test_released_pair_lock_inputs_pin_each_declared_package_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "migrations"
    state = root / ".parity/workspace"
    state.mkdir(parents=True)
    workspace = ResolvedWorkspace(
        path=root / "parity.workspace.toml",
        root=root,
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package="candidate-lib==2.0.0",
        candidate_path=None,
        candidate_install_mode=None,
        candidate_extras=(),
        reference_python="3.11",
        candidate_python="3.12",
        config=root / "parity.toml",
        manifest=root / "migration.toml",
        report_dir=root / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        operation: str,
        failure_log: Path,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, operation, failure_log
        commands.append(command)
        output = Path(command[command.index("--output-file") + 1])
        output.write_text("candidate-lib==1.9.0\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("parity.migration_workspace._run_checked", fake_run)
    for side in ("reference", "candidate"):
        migration_workspace_module._compile_lane_lock(
            workspace,
            workspace.lanes[0],
            side=side,
            uv="/tools/uv",
            state_root=state,
            refresh=False,
        )

    assert (
        (state / "inputs/default.reference.in")
        .read_text(encoding="utf-8")
        .endswith("candidate-lib==1.9.0\n")
    )
    assert (
        (state / "inputs/default.candidate.in")
        .read_text(encoding="utf-8")
        .endswith("candidate-lib==2.0.0\n")
    )
    assert commands[0][commands[0].index("--python-version") + 1] == "3.11"
    assert commands[1][commands[1].index("--python-version") + 1] == "3.12"


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
    (locks / "requirements.default.reference.txt").write_text(
        "# Deliberately empty: this smoke test exercises tox without resolving packages.\n",
        encoding="utf-8",
    )
    workspace = ResolvedWorkspace(
        path=project / "parity.workspace.toml",
        root=project,
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package=None,
        candidate_path=project,
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python=f"{sys.version_info.major}.{sys.version_info.minor}",
        candidate_python=f"{sys.version_info.major}.{sys.version_info.minor}",
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
    monkeypatch.setattr("parity.migration_workspace._tool", lambda name: (tools[name],))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        assert "shell" not in kwargs
        if command[0] == "/tools/uv":
            output = Path(command[command.index("--output-file") + 1])
            output.write_text(
                "pyarrow==20.0.0 \\\n"
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
    assert len([command for command, _ in calls if command[0] == "/tools/uv"]) == 4
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
    reference_input = (tmp_path / ".parity/workspace/inputs/release.reference.in").read_text(
        encoding="utf-8"
    )
    candidate_input = (tmp_path / ".parity/workspace/inputs/release.candidate.in").read_text(
        encoding="utf-8"
    )
    assert reference_input == (
        "# Generated by Parity. Do not edit.\npyarrow>=16\ncandidate-lib==1.9.0\n"
    )
    assert candidate_input == (
        "# Generated by Parity. Do not edit.\npyarrow>=16\ncandidate-lib==2.0.0\n"
    )
    assert prepared.tox_config == tmp_path / ".parity/workspace/tox.toml"
    assert (tmp_path / ".parity/.gitignore").read_text(encoding="utf-8") == "*\n"
    assert not (tmp_path / ".gitignore").exists()


def test_setup_missing_optional_tool_is_actionable(tmp_path: Path, monkeypatch) -> None:
    workspace_path = _project(tmp_path)
    monkeypatch.setattr("parity.migration_workspace.importlib.util.find_spec", lambda _name: None)

    with pytest.raises(WorkspaceError, match=r"parity-check\[workspace\]"):
        setup_workspace(workspace_path)


def test_workspace_tools_use_the_controller_interpreter_instead_of_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "parity.migration_workspace.shutil.which",
        lambda name: f"/untrusted/path/{name}",
    )
    monkeypatch.setattr(
        "parity.migration_workspace.importlib.util.find_spec",
        lambda _name: object(),
    )

    assert migration_workspace_module._tool("uv") == (sys.executable, "-I", "-m", "uv")
    assert migration_workspace_module._tool("tox") == (sys.executable, "-I", "-m", "tox")


def test_source_revision_tracks_head_dirty_state_and_worktree_content(tmp_path: Path) -> None:
    source = _git_checkout(tmp_path / "checkout", version="1.0.0", value="old")

    clean = migration_workspace_module._source_revision(source)
    same = migration_workspace_module._source_revision(source)

    assert clean == same
    assert clean.dirty is False
    assert len(clean.git_head) == 40
    assert len(clean.source_sha256) == 64

    (source / "candidate_lib/__init__.py").write_text('VALUE = "new"\n', encoding="utf-8")
    dirty = migration_workspace_module._source_revision(source)

    assert dirty.git_head == clean.git_head
    assert dirty.dirty is True
    assert dirty.source_sha256 != clean.source_sha256
    assert str(tmp_path) not in dirty.model_dump_json()


def test_source_revision_rejects_uninitialized_submodule(tmp_path: Path) -> None:
    source = _git_checkout(tmp_path / "checkout", version="1.0.0", value="old")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{head},vendor"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (source / "vendor").mkdir()
    subprocess.run(
        ["git", "commit", "-qm", "declare submodule"],
        cwd=source,
        check=True,
        capture_output=True,
    )

    with pytest.raises(WorkspaceError, match="uninitialized Git submodule"):
        migration_workspace_module._source_revision(source)


def test_source_install_probe_accepts_declared_source_and_rejects_shadowing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    package = source / "candidate_lib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    metadata = tmp_path / "site/candidate_lib-1.0.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: candidate-lib\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    (metadata / "direct_url.json").write_text(
        json.dumps({"url": source.as_uri(), "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    neutral = tmp_path / "harness"
    neutral.mkdir()
    pythonpath = os.pathsep.join([str(tmp_path / "site"), str(source)])

    migration_workspace_module._validate_source_install(
        Path(sys.executable),
        source=source,
        subject="candidate-lib",
        side="candidate",
        workspace_root=neutral,
        environment={"PYTHONPATH": pythonpath},
    )

    shadow = tmp_path / "shadow/candidate_lib"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="missing or shadowed"):
        migration_workspace_module._validate_source_install(
            Path(sys.executable),
            source=source,
            subject="candidate-lib",
            side="candidate",
            workspace_root=neutral,
            environment={
                "PYTHONPATH": os.pathsep.join(
                    [str(tmp_path / "shadow"), str(tmp_path / "site"), str(source)]
                )
            },
        )


def test_source_import_discovery_ignores_flat_layout_tests_and_scripts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "core_lib").mkdir(parents=True)
    (source / "core_lib/__init__.py").write_text("", encoding="utf-8")
    for directory in ("tests", "scripts", "tools"):
        root = source / directory
        root.mkdir()
        (root / "helper.py").write_text("", encoding="utf-8")
    (source / "noxfile.py").write_text("", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "core-lib"\nversion = "1"\n', encoding="utf-8"
    )

    assert migration_workspace_module._source_import_names(source, "core-lib") == ("core_lib",)


def test_source_import_discovery_supports_pep420_namespace_packages(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "src/acme/widgets"
    package.mkdir(parents=True)
    (package / "transform.py").write_text("", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "acme-widgets"\nversion = "1"\n', encoding="utf-8"
    )

    assert migration_workspace_module._source_import_names(source, "acme-widgets") == ("acme",)


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
        reference_package="candidate-lib==1.9.0",
        candidate_path=Path(".."),
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
    lock = tmp_path / ".parity/workspace/locks/requirements.default.reference.txt"
    lock.parent.mkdir(parents=True)
    previous = "parity-check==0.9.2\n"
    lock.write_text(previous, encoding="utf-8")
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace._tool", lambda name: (tools[name],))

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
    assert ".parity/workspace/logs/resolve-default-reference.log" in str(caught.value)
    assert lock.read_text(encoding="utf-8") == previous
    private_log = tmp_path / ".parity/workspace/logs/resolve-default-reference.log"
    assert "/private/project/source" in private_log.read_text(encoding="utf-8")
    assert "token@example.invalid" in private_log.read_text(encoding="utf-8")


def test_private_state_symlink_cannot_escape_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace_path = _project(tmp_path)
    # Keep reports outside .parity so setup reaches the private-state guard itself.
    write_workspace(
        workspace_path,
        reference_package="candidate-lib==1.9.0",
        candidate_package="candidate-lib==2.0.0",
        python_version="3.12",
        report_dir=Path("reports"),
        force=True,
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / ".parity").symlink_to(outside, target_is_directory=True)
    tools = {"uv": "/tools/uv", "tox": "/tools/tox"}
    monkeypatch.setattr("parity.migration_workspace._tool", lambda name: (tools[name],))

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
    monkeypatch.setattr("parity.migration_workspace._tool", lambda name: (tools[name],))

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
        reference_package="Candidate_Lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package=None,
        candidate_path=project.parent / f"{project.name}-candidate-lib",
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
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
    monkeypatch.setattr(
        "parity.migration_workspace._validate_local_source_installs",
        lambda _setup, _config: None,
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
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package=None,
        candidate_path=tmp_path / "candidate-src" / "candidate-lib",
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
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


def test_released_pair_binds_both_exact_runtime_versions_without_mutating_config(
    tmp_path: Path,
) -> None:
    workspace = ResolvedWorkspace(
        path=tmp_path / "parity.workspace.toml",
        root=tmp_path,
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package="candidate-lib==2.0.0",
        candidate_path=None,
        candidate_install_mode=None,
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
        config=tmp_path / "parity.toml",
        manifest=tmp_path / "migration.toml",
        report_dir=tmp_path / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    config = _config()

    effective = migration_workspace_module._bind_subject_distribution(workspace, config)

    case = effective.cases[0]
    assert case.reference.required_distributions == {"candidate-lib": "==1.9.0"}
    assert case.candidate.required_distributions == {"candidate-lib": "==2.0.0"}
    assert case.reference.record_distributions == ["candidate-lib"]
    assert case.candidate.record_distributions == ["candidate-lib"]
    assert case.reference.workdir == tmp_path
    assert case.candidate.workdir == tmp_path
    assert config.cases[0].reference.required_distributions == {}
    assert config.cases[0].candidate.required_distributions == {}

    config.cases[0].candidate.required_distributions = {"candidate-lib": "<2"}
    with pytest.raises(WorkspaceError, match="candidate runtime requirements conflict"):
        migration_workspace_module._bind_subject_distribution(workspace, config)


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
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package=None,
        candidate_path=candidate,
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
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
        reference_package="candidate-lib==1.9.0",
        reference_path=None,
        reference_install_mode=None,
        reference_extras=(),
        candidate_package=None,
        candidate_path=candidate,
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
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
    monkeypatch.setattr(
        "parity.migration_workspace._validate_local_source_installs",
        lambda _setup, _config: None,
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


def test_local_run_writes_path_free_source_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _git_checkout(tmp_path / "reference", version="1.0.0", value="old")
    candidate = _git_checkout(tmp_path / "candidate", version="2.0.0", value="new")
    root = tmp_path / "harness"
    workspace = ResolvedWorkspace(
        path=root / "parity.workspace.toml",
        root=root,
        reference_package=None,
        reference_path=reference,
        reference_install_mode="editable",
        reference_extras=(),
        candidate_package=None,
        candidate_path=candidate,
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
        config=root / "parity.toml",
        manifest=root / "migration.toml",
        report_dir=root / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    _write_resolved_workspace_document(workspace)
    environment = LaneEnvironment(
        name="default",
        reference_env="default-reference",
        candidate_env="default-candidate",
        reference_python=_executable(root / ".parity/workspace/envs/ref/python"),
        candidate_python=_executable(root / ".parity/workspace/envs/candidate/python"),
    )
    setup = WorkspaceSetup(
        workspace=workspace,
        tox_config=root / ".parity/workspace/tox.toml",
        lanes=(environment,),
    )
    config = _config()
    manifest = MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])])
    sentinel = cast(MigrationResult, object())
    monkeypatch.setattr("parity.migration_workspace.load_workspace", lambda _path: workspace)
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: config)
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest", lambda _path: manifest
    )
    monkeypatch.setattr(
        "parity.migration_workspace._setup_resolved_workspace",
        lambda _workspace, *, refresh_locks: setup,
    )
    monkeypatch.setattr(
        "parity.migration_workspace._validate_local_source_installs",
        lambda _setup, _config: None,
    )
    monkeypatch.setattr(
        "parity.migration_workspace._assert_lane_source_provenance",
        lambda _result, _expected: None,
    )
    monkeypatch.setattr("parity.migration_workspace.run_migration", lambda *_args: sentinel)

    def fake_write(_result: MigrationResult, destination: str | Path) -> Path:
        path = Path(destination)
        path.write_text('{"status":"passed"}\n', encoding="utf-8")
        return path

    monkeypatch.setattr("parity.migration_workspace.write_migration_json", fake_write)

    result = run_workspace(workspace.path)

    assert result.source_provenance == workspace.report_dir / "source-provenance.json"
    payload = json.loads(result.source_provenance.read_text(encoding="utf-8"))
    encoded = json.dumps(payload)
    assert payload["distribution"] == "candidate-lib"
    assert payload["reference"]["dirty"] is False
    assert payload["candidate"]["dirty"] is False
    assert len(payload["reference"]["git_head"]) == 40
    assert len(payload["candidate"]["source_sha256"]) == 64
    assert str(tmp_path) not in encoded
    assert "reference_path" not in encoded


def test_lane_evidence_must_match_driver_source_provenance(tmp_path: Path) -> None:
    reference = _git_checkout(tmp_path / "reference", version="1.0.0", value="old")
    candidate = _git_checkout(tmp_path / "candidate", version="1.0.0", value="new")
    expected = migration_workspace_module.WorkspaceSourceProvenance(
        distribution="candidate-lib",
        reference=migration_workspace_module._source_revision(reference),
        candidate=migration_workspace_module._source_revision(candidate),
    )

    def identity(
        revision: migration_workspace_module.SourceRevision,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            name="candidate-lib",
            kind="git-worktree-v1",
            revision=revision.git_head,
            dirty=revision.dirty,
            sha256=revision.source_sha256,
        )

    provenance = SimpleNamespace(
        reference=SimpleNamespace(identities=(identity(expected.reference),)),
        candidate=SimpleNamespace(identities=(identity(expected.candidate),)),
    )
    case = SimpleNamespace(status=migration_workspace_module.Status.FAILED, provenance=provenance)
    result = cast(
        MigrationResult,
        SimpleNamespace(suite=SimpleNamespace(cases=[case])),
    )

    migration_workspace_module._assert_lane_source_provenance(result, expected)

    provenance.candidate.identities[0].sha256 = "0" * 64
    with pytest.raises(WorkspaceError, match="candidate target source provenance"):
        migration_workspace_module._assert_lane_source_provenance(result, expected)


@pytest.mark.skipif(os.name == "nt", reason="uses small POSIX target-Python launchers")
def test_same_version_worktree_mutation_blocks_finding_replay_before_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay verifies source content, not only unchanged package versions."""

    monkeypatch.chdir(tmp_path)
    harness = tmp_path / "harness"
    harness.mkdir()
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"value": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)

    endpoint_specs: list[CallableSpec] = []
    sources: dict[str, Path] = {}
    imported_markers: list[Path] = []
    invoked_markers: list[Path] = []
    for side, incompatible in (("reference", False), ("candidate", True)):
        source = _git_checkout(tmp_path / side, version="1.0.0", value=side)
        sources[side] = source
        imported = tmp_path / f"{side}-imported.txt"
        invoked = tmp_path / f"{side}-invoked.txt"
        imported_markers.append(imported)
        invoked_markers.append(invoked)
        returned = (
            "frame.append_column('candidate_only', frame.column(0))" if incompatible else "frame"
        )
        (source / "candidate_lib/__init__.py").write_text(
            "from pathlib import Path\n"
            f"IMPORTED = Path({str(imported)!r})\n"
            f"INVOKED = Path({str(invoked)!r})\n"
            "IMPORTED.write_text('yes', encoding='utf-8')\n"
            "def transform(frame):\n"
            "    INVOKED.write_text('yes', encoding='utf-8')\n"
            f"    return {returned}\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "candidate_lib/__init__.py"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "add target"],
            cwd=source,
            check=True,
            capture_output=True,
        )

        site = tmp_path / f"{side}-site"
        metadata = site / "candidate_lib-1.0.0.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: candidate-lib\nVersion: 1.0.0\n",
            encoding="utf-8",
        )
        (metadata / "direct_url.json").write_text(
            json.dumps({"url": source.as_uri(), "dir_info": {"editable": True}}),
            encoding="utf-8",
        )
        launcher = tmp_path / f"{side}-python"
        pythonpath = os.pathsep.join((str(site), str(source)))
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            f"os.environ['PYTHONPATH'] = {pythonpath!r}\n"
            "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        endpoint_specs.append(
            CallableSpec(
                target="candidate_lib:transform",
                adapter="arrow",
                python=launcher,
                workdir=harness,
                record_distributions=["candidate-lib"],
            )
        )

    case = CaseConfig(
        name="same-version-source",
        reference=endpoint_specs[0],
        candidate=endpoint_specs[1],
        fixture=fixture,
        generation=GenerationConfig(
            max_examples=1,
            adversarial_examples=False,
            search=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )
    initial = run_migration(
        MigrationManifest(units=[MigrationUnit(id="source", cases=[case.name])]),
        ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]),
    )

    assert initial.status is Status.FAILED
    expected = migration_workspace_module.WorkspaceSourceProvenance(
        distribution="candidate-lib",
        reference=migration_workspace_module._source_revision(sources["reference"]),
        candidate=migration_workspace_module._source_revision(sources["candidate"]),
    )
    migration_workspace_module._assert_lane_source_provenance(initial, expected)
    lane_report = migration_report_payload(initial)
    for side in ("reference", "candidate"):
        identities = lane_report["parity"]["cases"][0]["provenance"][side]["identities"]
        assert identities[0]["name"] == "candidate-lib"
        assert len(identities[0]["sha256"]) == 64
        assert str(tmp_path) not in json.dumps(identities)

    artifact = initial.suite.cases[0].failures[0].artifact
    assert artifact is not None
    replay_contract = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    for side in ("reference", "candidate"):
        runtime = replay_contract["expected_runtime"][side]
        assert runtime["identities"][0]["name"] == "candidate-lib"
        assert len(runtime["identities"][0]["sha256"]) == 64
        assert str(tmp_path) not in json.dumps(runtime["identities"])

    for marker in (*imported_markers, *invoked_markers):
        marker.unlink()
    candidate_module = tmp_path / "candidate/candidate_lib/__init__.py"
    candidate_module.write_text(
        candidate_module.read_text(encoding="utf-8") + "# same version, different source\n",
        encoding="utf-8",
    )

    replayed = replay_artifact(artifact)

    assert replayed.status is Status.ERROR
    assert replayed.cases[0].failures[0].source == "replay:provenance"
    assert replayed.cases[0].provenance is not None
    assert replayed.cases[0].provenance.verification == "drifted"
    assert all(not marker.exists() for marker in imported_markers)
    assert all(not marker.exists() for marker in invoked_markers)


def test_local_run_invalidates_results_when_a_checkout_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _git_checkout(tmp_path / "reference", version="1.0.0", value="old")
    candidate = _git_checkout(tmp_path / "candidate", version="2.0.0", value="new")
    root = tmp_path / "harness"
    workspace = ResolvedWorkspace(
        path=root / "parity.workspace.toml",
        root=root,
        reference_package=None,
        reference_path=reference,
        reference_install_mode="editable",
        reference_extras=(),
        candidate_package=None,
        candidate_path=candidate,
        candidate_install_mode="editable",
        candidate_extras=(),
        reference_python="3.12",
        candidate_python="3.12",
        config=root / "parity.toml",
        manifest=root / "migration.toml",
        report_dir=root / ".parity/workspace/reports",
        lanes=(ResolvedWorkspaceLane("default", None),),
    )
    _write_resolved_workspace_document(workspace)
    environment = LaneEnvironment(
        name="default",
        reference_env="default-reference",
        candidate_env="default-candidate",
        reference_python=_executable(root / "envs/reference/python"),
        candidate_python=_executable(root / "envs/candidate/python"),
    )
    setup = WorkspaceSetup(workspace=workspace, tox_config=root / "tox.toml", lanes=(environment,))
    config = _config()
    manifest = MigrationManifest(units=[MigrationUnit(id="orders", cases=["orders"])])
    workspace.report_dir.mkdir(parents=True)
    stale_report = workspace.report_dir / "default.json"
    stale_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    stale_sources = workspace.report_dir / "source-provenance.json"
    stale_sources.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "distribution": "candidate-lib",
                "reference": {
                    "git_head": "a" * 40,
                    "dirty": False,
                    "source_sha256": "b" * 64,
                },
                "candidate": {
                    "git_head": "c" * 40,
                    "dirty": False,
                    "source_sha256": "d" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("parity.migration_workspace.load_workspace", lambda _path: workspace)
    monkeypatch.setattr("parity.migration_workspace.load_config", lambda _path: config)
    monkeypatch.setattr(
        "parity.migration_workspace.load_migration_manifest", lambda _path: manifest
    )
    monkeypatch.setattr(
        "parity.migration_workspace._setup_resolved_workspace",
        lambda _workspace, *, refresh_locks: setup,
    )
    monkeypatch.setattr(
        "parity.migration_workspace._validate_local_source_installs",
        lambda _setup, _config: None,
    )

    def mutate_source(*_args: object) -> MigrationResult:
        (candidate / "candidate_lib/__init__.py").write_text(
            'VALUE = "changed during run"\n', encoding="utf-8"
        )
        return cast(MigrationResult, object())

    monkeypatch.setattr("parity.migration_workspace.run_migration", mutate_source)
    monkeypatch.setattr(
        "parity.migration_workspace.write_migration_json",
        lambda *_args: pytest.fail("a mutated-source report must not be written"),
    )

    with pytest.raises(WorkspaceError, match="changed during managed execution"):
        run_workspace(workspace.path)

    assert not stale_report.exists()
    assert not stale_sources.exists()


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
            "--reference-package",
            "candidate-lib==1.9.0",
            "--workspace",
            str(workspace),
            "--candidate-path",
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
    assert "parity migration validate" in result.stdout
    assert "active pair declared" in result.stdout
    assert "created starter ledger" in result.stdout
    document = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert document["version"] == 3
    assert document["reference_package"] == "candidate-lib==1.9.0"
    assert document["candidate_path"] == "candidate"
    assert [lane["name"] for lane in document["lanes"]] == ["release", "current"]
    assert document["report_dir"] == ".parity/workspace/reports"
    manifest = tomllib.loads((tmp_path / "migration.toml").read_text(encoding="utf-8"))
    assert manifest["units"][0]["id"] == "core-regression"
    assert not (tmp_path / ".parity").exists()


def test_migration_init_cli_declares_local_worktree_pair(tmp_path: Path, monkeypatch) -> None:
    write_starter(tmp_path / "migrations/parity.toml")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-path",
            "reference-worktree",
            "--candidate-path",
            "candidate-worktree",
            "--reference-python",
            "3.8",
            "--candidate-python",
            "3.12",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "local pair declared" in result.stdout
    document = tomllib.loads(
        (tmp_path / "migrations/parity.workspace.toml").read_text(encoding="utf-8")
    )
    assert document["version"] == 3
    assert "reference_package" not in document
    assert document["reference_path"] == "../reference-worktree"
    assert document["candidate_path"] == "../candidate-worktree"
    assert "python" not in document
    assert document["reference_python"] == "3.8"
    assert document["candidate_python"] == "3.12"

    both = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--reference-path",
            "reference-worktree",
            "--force",
        ],
    )
    assert both.exit_code == 2
    assert "exactly one" in both.stderr


def test_migration_init_cli_scaffolds_released_pair_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "windowed.csv"
    fixture.write_text("value\n1\n2\n3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "more-itertools==8.14.0",
            "--candidate-package",
            "more-itertools==9.0.0",
            "--target",
            "migration_adapters:windowed_contract",
            "--fixture",
            fixture.name,
            "--case-name",
            "windowed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "created migration contract" in result.stdout
    assert "released pair declared" in result.stdout
    workspace_path = tmp_path / "migrations/parity.workspace.toml"
    workspace = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    config = tomllib.loads((tmp_path / "migrations/parity.toml").read_text(encoding="utf-8"))
    manifest = tomllib.loads((tmp_path / "migrations/migration.toml").read_text(encoding="utf-8"))
    assert workspace["version"] == 3
    assert workspace["reference_package"] == "more-itertools==8.14.0"
    assert workspace["candidate_package"] == "more-itertools==9.0.0"
    assert "candidate_path" not in workspace
    assert config["cases"][0]["name"] == "windowed"
    assert config["cases"][0]["fixture"] == "../windowed.csv"
    assert config["cases"][0]["reference"]["target"] == ("migration_adapters:windowed_contract")
    assert config["cases"][0]["candidate"]["target"] == ("migration_adapters:windowed_contract")
    assert "workdir" not in config["cases"][0]["reference"]
    assert "workdir" not in config["cases"][0]["candidate"]
    assert manifest["units"][0]["cases"] == ["windowed"]


def test_agent_scaffold_json_requires_explicit_review_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    initialized = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "more-itertools==8.14.0",
            "--candidate-package",
            "more-itertools==9.0.0",
            "--scaffold",
            "--json",
        ],
    )

    assert initialized.exit_code == 0, initialized.output
    assert initialized.stderr == ""
    payload = json.loads(initialized.stdout)
    assert payload["status"] == "needs_review"
    assert [item["kind"] for item in payload["created_files"]] == [
        "workspace",
        "config",
        "manifest",
        "adapter",
        "fixture",
        "checklist",
    ]
    assert payload["next_commands"][0]["argv"] == [
        "parity",
        "migration",
        "validate",
        "--workspace",
        "migrations/parity.workspace.toml",
        "--json",
    ]
    workspace = tomllib.loads(
        (tmp_path / "migrations/parity.workspace.toml").read_text(encoding="utf-8")
    )
    assert workspace["version"] == 3
    assert workspace["checklist"] == "migration.checklist.json"
    assert not (tmp_path / "migrations/.parity").exists()

    pending = runner.invoke(cli.app, ["migration", "validate", "--json"])
    assert pending.exit_code == 1
    pending_payload = json.loads(pending.stdout)
    assert pending_payload["status"] == "needs_review"
    assert {issue["code"] for issue in pending_payload["issues"]} == {
        f"checklist.{identifier.value}" for identifier in ChecklistItemId
    }
    assert not (tmp_path / "migrations/.parity").exists()

    checklist_path = tmp_path / "migrations/migration.checklist.json"
    checklist = ContractChecklist.model_validate_json(checklist_path.read_text(encoding="utf-8"))
    resolved = checklist.resolving(*ChecklistItemId)
    checklist_path.write_text(resolved.model_dump_json(indent=2) + "\n", encoding="utf-8")

    ready = runner.invoke(cli.app, ["migration", "validate", "--json"])
    assert ready.exit_code == 0, ready.output
    ready_payload = json.loads(ready.stdout)
    assert ready_payload["status"] == "ready"
    assert ready_payload["next_commands"][0]["argv"][1:3] == ["migration", "run"]
    assert not (tmp_path / "migrations/.parity").exists()


def test_agent_scaffold_is_all_or_nothing_and_never_overwrites_authored_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    adapter = migrations / "migration_adapters.py"
    adapter.write_text("# reviewed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--scaffold",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "error"
    assert adapter.read_text(encoding="utf-8") == "# reviewed\n"
    assert not (migrations / "parity.toml").exists()
    assert not (migrations / "migration.toml").exists()
    assert not (migrations / "parity.workspace.toml").exists()
    assert not (migrations / "migration.checklist.json").exists()
    assert not (migrations / "fixtures/input.json").exists()


def test_migration_init_cli_supports_config_above_workspace_for_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fixture.csv"
    fixture.write_text("value\n1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--config",
            "parity.toml",
            "--target",
            "migration_adapters:transform",
            "--fixture",
            fixture.name,
        ],
    )

    assert result.exit_code == 0, result.output
    workspace_path = tmp_path / "migrations/parity.workspace.toml"
    workspace = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    config = tomllib.loads((tmp_path / "parity.toml").read_text(encoding="utf-8"))
    assert workspace["config"] == "../parity.toml"
    assert config["cases"][0]["reference"]["workdir"] == "migrations"
    assert config["cases"][0]["candidate"]["workdir"] == "migrations"
    assert load_workspace(workspace_path).config == (tmp_path / "parity.toml").resolve()


def test_migration_init_cli_rolls_back_config_outside_replay_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fixture.csv"
    fixture.write_text("value\n1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--config",
            "configs/parity.toml",
            "--target",
            "migration_adapters:transform",
            "--fixture",
            fixture.name,
        ],
    )

    assert result.exit_code == 2
    assert "workspace config must contain" in result.stderr
    assert not (tmp_path / "configs/parity.toml").exists()
    assert not (tmp_path / "migrations/migration.toml").exists()
    assert not (tmp_path / "migrations/parity.workspace.toml").exists()


def test_migration_init_cli_side_targets_override_shared_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fixture.csv"
    fixture.write_text("value\n1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--target",
            "shared:transform",
            "--reference-target",
            "old:transform",
            "--candidate-target",
            "new:transform",
            "--fixture",
            fixture.name,
        ],
    )

    assert result.exit_code == 0, result.output
    config = tomllib.loads((tmp_path / "migrations/parity.toml").read_text(encoding="utf-8"))
    assert config["cases"][0]["reference"]["target"] == "old:transform"
    assert config["cases"][0]["candidate"]["target"] == "new:transform"


def test_migration_init_cli_never_partially_scaffolds_or_overwrites_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fixture.csv"
    fixture.write_text("value\n1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    incomplete = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--target",
            "adapters:transform",
        ],
    )

    assert incomplete.exit_code == 2
    assert "supply --fixture" in incomplete.stderr
    assert not (tmp_path / "migrations/parity.toml").exists()
    assert not (tmp_path / "migrations/migration.toml").exists()
    assert not (tmp_path / "migrations/parity.workspace.toml").exists()

    write_starter(tmp_path / "migrations/parity.toml")
    original = (tmp_path / "migrations/parity.toml").read_bytes()
    existing = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--target",
            "adapters:transform",
            "--fixture",
            fixture.name,
        ],
    )

    assert existing.exit_code == 2
    assert "reviewed Parity config already exists" in existing.stderr
    assert (tmp_path / "migrations/parity.toml").read_bytes() == original
    assert not (tmp_path / "migrations/migration.toml").exists()
    assert not (tmp_path / "migrations/parity.workspace.toml").exists()


def test_migration_init_cli_rejects_ambiguous_candidate_sources_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "migration",
            "init",
            "--reference-package",
            "candidate-lib==1",
            "--candidate-package",
            "candidate-lib==2",
            "--candidate-path",
            "candidate",
        ],
    )

    assert result.exit_code == 2
    assert "exactly one of --candidate-package or --candidate-path" in result.stderr
    assert not (tmp_path / "migrations").exists()


@pytest.mark.parametrize("legacy_flag", ["--reference", "--candidate"])
def test_migration_init_cli_rejects_legacy_source_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_flag: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        ["migration", "init", legacy_flag, "candidate-lib==1.9.0"],
    )

    assert result.exit_code == 2
    assert f"No such option: {legacy_flag}" in _normalized_cli_stderr(result.stderr)
    assert not (tmp_path / "migrations").exists()


def test_migration_advance_cli_rejects_legacy_reference_flag() -> None:
    result = runner.invoke(
        cli.app,
        ["migration", "advance", "--reference", "candidate-lib==2.0.0"],
    )

    assert result.exit_code == 2
    assert "No such option: --reference" in _normalized_cli_stderr(result.stderr)


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
        ["migration", "init", "--reference-package", "candidate-lib==1.9.0"],
    )

    assert initialized.exit_code == 0, initialized.output
    workspace = tmp_path / "migrations/parity.workspace.toml"
    document = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert document["version"] == 3
    assert document["reference_package"] == "candidate-lib==1.9.0"
    assert document["candidate_path"] == ".."
    assert document["config"] == "parity.toml"
    assert document["manifest"] == "migration.toml"
    assert (tmp_path / "migrations/migration.toml").is_file()

    report = tmp_path / "migrations/.parity/workspace/reports/default.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"passed"}\n', encoding="utf-8")
    advanced = runner.invoke(
        cli.app,
        ["migration", "advance", "--reference-package", "candidate-lib==2.0.0"],
    )

    assert advanced.exit_code == 0, advanced.output
    assert "previous active lane reports were invalidated" in advanced.stdout
    assert not report.exists()
    assert (
        tomllib.loads(workspace.read_text(encoding="utf-8"))["reference_package"]
        == "candidate-lib==2.0.0"
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
            "--reference-package",
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
