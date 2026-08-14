"""Private, reproducible environments for a declared migration workspace.

The workspace deliberately describes source locations; it never checks out or
changes source code.  ``tox`` owns environment lifecycle, while ``uv`` turns
the small human-authored inputs into one pinned lock per dependency lane.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import Field, field_validator, model_validator

from parity import __version__
from parity.config import load_config
from parity.migration import (
    MigrationResult,
    load_migration_manifest,
    run_migration,
    write_migration_json,
)
from parity.models import StrictModel

_WORKSPACE_NAME_PATTERN = r"^[A-Za-z0-9_.-]+$"
WorkspaceName = Annotated[str, Field(min_length=1, pattern=_WORKSPACE_NAME_PATTERN)]

_PYTHON_VERSION = re.compile(r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)$")
_ENV_PYTHON = re.compile(r"^env_python\s*=\s*(?P<path>.+?)\s*$", re.MULTILINE)
_PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?==[^\s\\]+")
_STATE_DIRECTORY = Path(".parity") / "workspace"
_WORKSPACE_EXTRA = "parity-check[workspace]"


def _reference_requirement(value: str) -> tuple[Requirement, Specifier, tuple[str, ...]]:
    """Parse the intentionally narrow exact-reference requirement contract."""

    try:
        requirement = Requirement(value)
    except InvalidRequirement as exc:
        raise ValueError("reference must be a valid PEP 508 requirement") from exc
    specifiers = list(requirement.specifier)
    if requirement.url is not None or requirement.marker is not None or len(specifiers) != 1:
        raise ValueError("reference must contain exactly one unconditional == version")
    specifier = specifiers[0]
    if specifier.operator != "==" or "*" in specifier.version:
        raise ValueError("reference must contain exactly one non-wildcard == version")
    try:
        Version(specifier.version)
    except InvalidVersion as exc:
        raise ValueError("reference == operand must be a valid PEP 440 version") from exc

    extras: tuple[str, ...] = ()
    opening = value.find("[")
    if opening >= 0:
        closing = value.find("]", opening)
        raw_extras = tuple(part.strip() for part in value[opening + 1 : closing].split(","))
        normalized = tuple(str(canonicalize_name(extra)) for extra in raw_extras)
        if len(normalized) != len(set(normalized)):
            raise ValueError("reference extras must be unique")
        extras = raw_extras
    return requirement, specifier, extras


class WorkspaceError(ValueError):
    """Raised when a migration workspace cannot be prepared safely."""


class WorkspaceLane(StrictModel):
    """One shared dependency lane used by a reference/candidate pair."""

    name: WorkspaceName
    requirements: Path | None = None


class MigrationWorkspace(StrictModel):
    """Human-authored, versioned migration workspace document."""

    version: Literal[1] = 1
    reference: str
    candidate: Path = Path(".")
    python: str
    config: Path = Path("parity.toml")
    manifest: Path = Path("migration.toml")
    report_dir: Path = Path(".parity/workspace/reports")
    lanes: list[WorkspaceLane] = Field(
        default_factory=lambda: [WorkspaceLane(name="default")],
        min_length=1,
    )

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, requirement: str) -> str:
        try:
            _reference_requirement(requirement)
        except ValueError as exc:
            raise ValueError(
                "reference must be one exact requirement such as package==1.2.3 "
                f"or package[extra]==1.2.3 ({exc})"
            ) from exc
        return requirement

    @field_validator("python")
    @classmethod
    def validate_python(cls, version: str) -> str:
        match = _PYTHON_VERSION.fullmatch(version)
        if match is None:
            raise ValueError("python must be a major.minor version such as 3.12")
        if (int(match.group("major")), int(match.group("minor"))) < (3, 11):
            raise ValueError("python must satisfy Parity's Python >=3.11 requirement")
        return version

    @model_validator(mode="after")
    def unique_lanes(self) -> MigrationWorkspace:
        names = [lane.name for lane in self.lanes]
        if len(names) != len(set(names)):
            raise ValueError("workspace lane names must be unique")
        return self

    @property
    def reference_extras(self) -> tuple[str, ...]:
        """Return extras requested from both package implementations."""

        _, _, extras = _reference_requirement(self.reference)
        return extras


@dataclass(frozen=True)
class ResolvedWorkspaceLane:
    """A dependency lane with paths anchored beside the workspace file."""

    name: str
    requirements: Path | None


@dataclass(frozen=True)
class ResolvedWorkspace:
    """Validated absolute paths for one workspace document."""

    path: Path
    root: Path
    reference: str
    reference_extras: tuple[str, ...]
    candidate: Path
    candidate_package: Literal["editable", "editable-legacy"]
    python: str
    config: Path
    manifest: Path
    report_dir: Path
    lanes: tuple[ResolvedWorkspaceLane, ...]


@dataclass(frozen=True)
class LaneEnvironment:
    """The two worker interpreters provisioned for one lane."""

    name: str
    reference_env: str
    candidate_env: str
    reference_python: Path
    candidate_python: Path


@dataclass(frozen=True)
class WorkspaceSetup:
    """Provisioning result consumed by migration execution."""

    workspace: ResolvedWorkspace
    tox_config: Path
    lanes: tuple[LaneEnvironment, ...]


@dataclass(frozen=True)
class LaneMigrationResult:
    """One migration result bound to its dependency lane."""

    name: str
    result: MigrationResult
    report: Path


@dataclass(frozen=True)
class WorkspaceRunResult:
    """All migration results produced by a workspace run."""

    lanes: tuple[LaneMigrationResult, ...]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _atomic_write_text(destination: Path, content: str, *, force: bool) -> None:
    """Publish a complete regular file without a check-then-write race."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            if os.path.lexists(destination) and not (
                destination.is_file() or destination.is_symlink()
            ):
                raise WorkspaceError("workspace destination is not a replaceable file")
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise FileExistsError(f"workspace already exists: {destination}") from None
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _path_text(path: Path) -> str:
    text = path.as_posix()
    if "\n" in text or "\r" in text:
        raise WorkspaceError("workspace paths cannot contain line breaks")
    return text


def render_workspace(workspace: MigrationWorkspace) -> str:
    """Render the stable human-authored workspace TOML contract."""

    lines = [
        "# parity.workspace.toml — generated by Parity",
        "# Relative paths are resolved beside this file.",
        "# Parity uses the candidate checkout in place; it never clones or modifies it.",
        "version = 1",
        f"reference = {_toml_string(workspace.reference)}",
        f"candidate = {_toml_string(_path_text(workspace.candidate))}",
        f"python = {_toml_string(workspace.python)}",
        f"config = {_toml_string(_path_text(workspace.config))}",
        f"manifest = {_toml_string(_path_text(workspace.manifest))}",
        f"report_dir = {_toml_string(_path_text(workspace.report_dir))}",
        "",
    ]
    for lane in workspace.lanes:
        lines.extend(
            [
                "[[lanes]]",
                f"name = {_toml_string(lane.name)}",
            ]
        )
        if lane.requirements is not None:
            lines.append(f"requirements = {_toml_string(_path_text(lane.requirements))}")
        lines.append("")
    return "\n".join(lines)


def parse_lane_options(values: Sequence[str]) -> list[WorkspaceLane]:
    """Parse repeatable ``NAME`` or ``NAME=REQUIREMENTS`` CLI values."""

    if not values:
        return [WorkspaceLane(name="default")]
    lanes: list[WorkspaceLane] = []
    for value in values:
        name, separator, requirement_path = value.partition("=")
        if separator and not requirement_path:
            raise WorkspaceError("--lane must be NAME or NAME=REQUIREMENTS")
        try:
            lanes.append(
                WorkspaceLane(
                    name=name,
                    requirements=Path(requirement_path) if separator else None,
                )
            )
        except ValueError as exc:
            raise WorkspaceError(f"invalid --lane value {value!r}: {exc}") from exc
    if len({lane.name for lane in lanes}) != len(lanes):
        raise WorkspaceError("--lane names must be unique")
    return lanes


def write_workspace(
    destination: str | Path = "parity.workspace.toml",
    *,
    reference: str,
    candidate: Path = Path("."),
    python_version: str | None = None,
    config: Path = Path("parity.toml"),
    manifest: Path = Path("migration.toml"),
    report_dir: Path = Path(".parity/workspace/reports"),
    lanes: Sequence[WorkspaceLane] = (),
    force: bool = False,
) -> Path:
    """Validate and atomically create one migration workspace document."""

    effective_lanes = list(lanes) or [WorkspaceLane(name="default")]
    try:
        workspace = MigrationWorkspace(
            reference=reference,
            candidate=candidate,
            python=python_version or f"{sys.version_info.major}.{sys.version_info.minor}",
            config=config,
            manifest=manifest,
            report_dir=report_dir,
            lanes=effective_lanes,
        )
    except ValueError as exc:
        raise WorkspaceError(f"invalid migration workspace: {exc}") from exc
    path = Path(destination)
    _atomic_write_text(path, render_workspace(workspace), force=force)
    return path


def _resolve_path(root: Path, value: Path) -> Path:
    path = (root / value).resolve() if not value.is_absolute() else value.resolve()
    _path_text(path)
    return path


def _reference_name(requirement: str) -> str:
    parsed, _, _ = _reference_requirement(requirement)
    return str(canonicalize_name(parsed.name))


def _candidate_name(candidate: Path) -> str:
    """Read a candidate distribution name without executing project code."""

    declared: list[str] = []
    pyproject = candidate / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise WorkspaceError("candidate pyproject.toml could not be read safely") from exc
        project = document.get("project")
        if isinstance(project, dict) and isinstance(project.get("name"), str):
            declared.append(project["name"])
        tool = document.get("tool")
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
                declared.append(poetry["name"])

    setup_cfg = candidate / "setup.cfg"
    if setup_cfg.is_file():
        parser = ConfigParser(interpolation=None)
        try:
            with setup_cfg.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (OSError, UnicodeError, ConfigParserError) as exc:
            raise WorkspaceError("candidate setup.cfg could not be read safely") from exc
        if parser.has_option("metadata", "name"):
            declared.append(parser.get("metadata", "name"))

    normalized = {str(canonicalize_name(name)) for name in declared if name.strip()}
    if len(normalized) != 1:
        detail = "is not declared statically" if not normalized else "is contradictory"
        raise WorkspaceError(
            f"candidate distribution name {detail}; managed setup requires project.name, "
            "tool.poetry.name or setup.cfg [metadata] name so it cannot install the wrong package. "
            "Provision worker interpreters explicitly for dynamic legacy metadata"
        )
    return normalized.pop()


def load_workspace(path: str | Path = "parity.workspace.toml") -> ResolvedWorkspace:
    """Load a workspace and validate every setup-time source input."""

    workspace_path = Path(path).resolve()
    try:
        raw: dict[str, Any] = tomllib.loads(workspace_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError(f"migration workspace not found: {workspace_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceError(f"invalid TOML in migration workspace: {exc}") from exc
    try:
        document = MigrationWorkspace.model_validate(raw)
    except ValueError as exc:
        raise WorkspaceError(f"invalid migration workspace: {exc}") from exc

    root = workspace_path.parent
    candidate = _resolve_path(root, document.candidate)
    if not candidate.is_dir():
        raise WorkspaceError("candidate must be an existing local checkout directory")
    if (candidate / "pyproject.toml").is_file():
        candidate_package: Literal["editable", "editable-legacy"] = "editable"
    elif (candidate / "setup.py").is_file():
        candidate_package = "editable-legacy"
    else:
        raise WorkspaceError(
            "candidate checkout needs pyproject.toml or setup.py; "
            "Parity does not fetch or modify source code"
        )
    reference_name = _reference_name(document.reference)
    candidate_name = _candidate_name(candidate)
    if candidate_name != reference_name:
        raise WorkspaceError(
            f"candidate distribution {candidate_name!r} does not match reference distribution "
            f"{reference_name!r}; refusing to install both packages into the candidate worker"
        )

    resolved_lanes: list[ResolvedWorkspaceLane] = []
    for lane in document.lanes:
        requirements = (
            _resolve_path(root, lane.requirements) if lane.requirements is not None else None
        )
        if requirements is not None and not requirements.is_file():
            raise WorkspaceError(
                f"requirements file for lane {lane.name!r} does not exist or is not a file"
            )
        resolved_lanes.append(ResolvedWorkspaceLane(lane.name, requirements))

    config = _resolve_path(root, document.config)
    manifest = _resolve_path(root, document.manifest)
    if not config.is_file():
        raise WorkspaceError(
            "the workspace Parity config is missing; migration init expects an existing parity.toml"
        )
    if not manifest.is_file():
        raise WorkspaceError(
            "the workspace migration ledger is missing; migration init expects an existing "
            "migration.toml"
        )

    report_dir = _resolve_path(root, document.report_dir)
    try:
        report_dir.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError("workspace report_dir must stay inside the workspace project") from exc

    return ResolvedWorkspace(
        path=workspace_path,
        root=root,
        reference=document.reference,
        reference_extras=document.reference_extras,
        candidate=candidate,
        candidate_package=candidate_package,
        python=document.python,
        config=config,
        manifest=manifest,
        report_dir=report_dir,
        lanes=tuple(resolved_lanes),
    )


def _state_root(workspace: ResolvedWorkspace) -> Path:
    """Create the private state root, rejecting redirects outside the project."""

    state = workspace.root / _STATE_DIRECTORY
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError("could not create private migration workspace state") from exc
    resolved = state.resolve()
    try:
        resolved.relative_to(workspace.root)
    except ValueError as exc:
        raise WorkspaceError(
            "private migration workspace state resolves outside the project"
        ) from exc
    if not resolved.is_dir():
        raise WorkspaceError("private migration workspace state is not a directory")
    return resolved


def _private_state_directory(state_root: Path, name: str) -> Path:
    """Create one fixed private child without following a pre-existing symlink."""

    if Path(name).name != name:  # pragma: no cover - internal fixed-name contract
        raise WorkspaceError("invalid private migration workspace directory")
    directory = state_root / name
    try:
        if os.path.lexists(directory) and directory.is_symlink():
            raise WorkspaceError(
                f"private migration workspace {name!r} directory cannot be a symlink"
            )
        directory.mkdir(parents=False, exist_ok=True)
    except WorkspaceError:
        raise
    except OSError as exc:
        raise WorkspaceError(
            f"could not create private migration workspace {name!r} directory"
        ) from exc
    if directory.is_symlink():
        raise WorkspaceError(f"private migration workspace {name!r} directory cannot be a symlink")
    resolved = directory.resolve()
    try:
        resolved.relative_to(state_root)
    except ValueError as exc:
        raise WorkspaceError(
            f"private migration workspace {name!r} directory resolves outside private state"
        ) from exc
    if not resolved.is_dir():
        raise WorkspaceError(f"private migration workspace {name!r} path is not a directory")
    return resolved


def _report_directory(workspace: ResolvedWorkspace) -> Path:
    """Create a report directory without following a redirect outside the project."""

    try:
        workspace.report_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError("could not create the workspace report directory") from exc
    resolved = workspace.report_dir.resolve()
    try:
        resolved.relative_to(workspace.root)
    except ValueError as exc:
        raise WorkspaceError("workspace report_dir resolves outside the project") from exc
    if not resolved.is_dir():
        raise WorkspaceError("workspace report_dir is not a directory")
    return resolved


def _lane_env_name(lane: str, side: Literal["reference", "candidate"]) -> str:
    return f"{lane}-{side}"


def render_tox_config(
    workspace: ResolvedWorkspace,
    *,
    state_root: Path,
) -> str:
    """Render deterministic private tox configuration for every lane pair."""

    env_names = [
        _lane_env_name(lane.name, side)
        for lane in workspace.lanes
        for side in ("reference", "candidate")
    ]
    env_dir = state_root / "envs"
    lines = [
        "# Generated by Parity. Do not edit.",
        'requires = ["tox>=4.44", "tox-uv>=1.29", "uv>=0.9.1"]',
        f"env_list = {_toml_array(env_names)}",
        f"work_dir = {_toml_string(_path_text(env_dir))}",
        f"package_root = {_toml_string(_path_text(workspace.candidate))}",
        "",
    ]
    for lane in workspace.lanes:
        lock = state_root / "locks" / f"requirements.{lane.name}.txt"
        reference_env = _lane_env_name(lane.name, "reference")
        candidate_env = _lane_env_name(lane.name, "candidate")
        common = [
            f"base_python = [{_toml_string(workspace.python)}]",
            'runner = "uv-venv-runner"',
            f"deps = [{_toml_string(f'-r{_path_text(lock)}')}]",
            "pass_env = []",
            "commands = []",
        ]
        lines.extend(
            [
                f"[env.{_toml_string(reference_env)}]",
                'description = "locked reference worker"',
                'package = "skip"',
                *common,
                "",
                f"[env.{_toml_string(candidate_env)}]",
                'description = "candidate checkout worker"',
                f"package = {_toml_string(workspace.candidate_package)}",
                *common,
                "constrain_package_deps = true",
                "use_frozen_constraints = true",
            ]
        )
        if workspace.reference_extras:
            lines.append(f"extras = {_toml_array(workspace.reference_extras)}")
        if workspace.candidate_package == "editable-legacy":
            lines.append("uv_seed = true")
        lines.append("")
    return "\n".join(lines)


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise WorkspaceError(
            f"migration environment support is not installed ({name} is missing); "
            f"install {_WORKSPACE_EXTRA!r}"
        )
    return found


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    operation: str,
    failure_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv without a shell and with data-safe failures."""

    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise WorkspaceError(f"{operation} could not start ({type(exc).__name__})") from exc
    if completed.returncode != 0:
        log_hint = ""
        if failure_log is not None:
            try:
                _atomic_write_text(
                    failure_log,
                    f"# Captured stdout\n{completed.stdout}\n# Captured stderr\n{completed.stderr}",
                    force=True,
                )
            except OSError:
                pass
            else:
                relative_hint = "/".join(failure_log.parts[-4:])
                log_hint = f"; details saved in {relative_hint}"
        raise WorkspaceError(f"{operation} failed (exit {completed.returncode}){log_hint}")
    if failure_log is not None:
        failure_log.unlink(missing_ok=True)
    return completed


def _failure_log(state_root: Path, name: str) -> Path:
    """Return one bounded path for captured private tool diagnostics."""

    if re.fullmatch(_WORKSPACE_NAME_PATTERN, name) is None:  # pragma: no cover - internal names
        raise WorkspaceError("invalid private migration workspace log name")
    return _private_state_directory(state_root, "logs") / f"{name}.log"


def _compile_lane_lock(
    workspace: ResolvedWorkspace,
    lane: ResolvedWorkspaceLane,
    *,
    uv: str,
    state_root: Path,
    refresh: bool,
) -> Path:
    inputs_dir = _private_state_directory(state_root, "inputs")
    locks_dir = _private_state_directory(state_root, "locks")
    generated_input = inputs_dir / f"{lane.name}.in"
    _atomic_write_text(
        generated_input,
        "# Generated by Parity. Do not edit.\n"
        f"parity-check=={__version__}\n"
        f"{workspace.reference}\n",
        force=True,
    )

    lock = locks_dir / f"requirements.{lane.name}.txt"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"requirements.{lane.name}.", suffix=".txt", dir=locks_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        if lock.is_file() and not refresh:
            shutil.copyfile(lock, temporary)
        command = [
            uv,
            "pip",
            "compile",
            "--format",
            "requirements-txt",
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--python-version",
            workspace.python,
            "--output-file",
            str(temporary),
        ]
        if refresh:
            command.append("--upgrade")
        if lane.requirements is not None:
            command.append(str(lane.requirements))
        command.append(str(generated_input))
        _run_checked(
            command,
            cwd=workspace.root,
            operation=f"dependency resolution for lane {lane.name!r}",
            failure_log=_failure_log(state_root, f"resolve-{lane.name}"),
        )
        try:
            rendered_lock = temporary.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkspaceError(
                f"dependency resolution for lane {lane.name!r} did not produce a valid lock"
            ) from exc
        if not any(_PINNED_REQUIREMENT.match(line) for line in rendered_lock.splitlines()):
            raise WorkspaceError(
                f"dependency resolution for lane {lane.name!r} produced no pinned requirements"
            )
        os.replace(temporary, lock)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return lock


def _tox_base(
    tox: str, workspace: ResolvedWorkspace, tox_config: Path, state_root: Path
) -> list[str]:
    return [
        tox,
        "--colored",
        "no",
        "-c",
        str(tox_config),
        "--root",
        str(workspace.candidate),
        "--workdir",
        str(_private_state_directory(state_root, "envs")),
    ]


def _query_env_python(
    tox: str,
    workspace: ResolvedWorkspace,
    tox_config: Path,
    state_root: Path,
    env_name: str,
) -> Path:
    completed = _run_checked(
        [
            *_tox_base(tox, workspace, tox_config, state_root),
            "config",
            "-e",
            env_name,
            "-k",
            "env_python",
        ],
        cwd=workspace.root,
        operation=f"interpreter discovery for environment {env_name!r}",
        failure_log=_failure_log(state_root, f"discover-{env_name}"),
    )
    matches = _ENV_PYTHON.findall(completed.stdout)
    if len(matches) != 1:
        raise WorkspaceError(
            f"tox did not report exactly one interpreter for environment {env_name!r}"
        )
    python = Path(matches[0])
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise WorkspaceError(f"tox reported an unusable interpreter for environment {env_name!r}")
    return python


def setup_workspace(
    path: str | Path = "parity.workspace.toml",
    *,
    refresh_locks: bool = False,
) -> WorkspaceSetup:
    """Resolve locks, prepare every tox environment and return worker paths."""

    workspace = load_workspace(path)
    uv = _tool("uv")
    tox = _tool("tox")
    state_root = _state_root(workspace)
    for child in ("inputs", "locks", "envs", "logs"):
        _private_state_directory(state_root, child)
    for lane in workspace.lanes:
        _compile_lane_lock(
            workspace,
            lane,
            uv=uv,
            state_root=state_root,
            refresh=refresh_locks,
        )
    tox_config = state_root / "tox.toml"
    _atomic_write_text(
        tox_config,
        render_tox_config(workspace, state_root=state_root),
        force=True,
    )
    _run_checked(
        [*_tox_base(tox, workspace, tox_config, state_root), "run", "--notest"],
        cwd=workspace.root,
        operation=(
            "tox environment setup; check the candidate packaging metadata, requested extras, "
            "and locked requirements"
        ),
        failure_log=_failure_log(state_root, "setup-environments"),
    )
    prepared_lanes: list[LaneEnvironment] = []
    for lane in workspace.lanes:
        candidate_python = _query_env_python(
            tox,
            workspace,
            tox_config,
            state_root,
            _lane_env_name(lane.name, "candidate"),
        )
        prepared_lanes.append(
            LaneEnvironment(
                name=lane.name,
                reference_env=_lane_env_name(lane.name, "reference"),
                candidate_env=_lane_env_name(lane.name, "candidate"),
                reference_python=_query_env_python(
                    tox,
                    workspace,
                    tox_config,
                    state_root,
                    _lane_env_name(lane.name, "reference"),
                ),
                candidate_python=candidate_python,
            )
        )
    return WorkspaceSetup(
        workspace=workspace,
        tox_config=tox_config,
        lanes=tuple(prepared_lanes),
    )


def run_workspace(
    path: str | Path = "parity.workspace.toml",
    *,
    refresh_locks: bool = False,
    progress: Callable[[Literal["setup", "lane", "complete"], str | None], None] | None = None,
) -> WorkspaceRunResult:
    """Prepare the workspace and run its migration gate in every lane."""

    if progress is not None:
        progress("setup", None)
    setup = setup_workspace(path, refresh_locks=refresh_locks)
    manifest = load_migration_manifest(setup.workspace.manifest)
    config = load_config(setup.workspace.config)
    report_dir = _report_directory(setup.workspace)
    lane_results: list[LaneMigrationResult] = []
    for lane in setup.lanes:
        if progress is not None:
            progress("lane", lane.name)
        effective = config.model_copy(deep=True)
        for case in effective.cases:
            case.reference.python = lane.reference_python
            case.candidate.python = lane.candidate_python
        result = run_migration(manifest, effective)
        report = write_migration_json(result, report_dir / f"{lane.name}.json")
        lane_results.append(LaneMigrationResult(name=lane.name, result=result, report=report))
        if progress is not None:
            progress("complete", lane.name)
    return WorkspaceRunResult(lanes=tuple(lane_results))


__all__ = [
    "LaneEnvironment",
    "LaneMigrationResult",
    "MigrationWorkspace",
    "ResolvedWorkspace",
    "ResolvedWorkspaceLane",
    "WorkspaceError",
    "WorkspaceLane",
    "WorkspaceRunResult",
    "WorkspaceSetup",
    "load_workspace",
    "parse_lane_options",
    "render_tox_config",
    "render_workspace",
    "run_workspace",
    "setup_workspace",
    "write_workspace",
]
