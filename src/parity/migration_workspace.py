"""Private, reproducible environments for a declared migration workspace.

The workspace deliberately describes exact package releases or source locations;
it never checks out or changes source code. ``tox`` owns environment lifecycle,
while ``uv`` turns the small human-authored inputs into one pinned lock per worker
and dependency lane.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
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
from packaging.specifiers import Specifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import Field, field_validator, model_validator

from parity.config import load_config
from parity.migration import (
    MigrationResult,
    load_migration_manifest,
    run_migration,
    write_migration_json,
)
from parity.models import CallableSpec, ParityConfig, Status, StrictModel

_WORKSPACE_NAME_PATTERN = r"^[A-Za-z0-9_.-]+$"
WorkspaceName = Annotated[str, Field(min_length=1, pattern=_WORKSPACE_NAME_PATTERN)]

_PYTHON_VERSION = re.compile(r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)$")
_ENV_PYTHON = re.compile(r"^env_python\s*=\s*(?P<path>.+?)\s*$", re.MULTILINE)
_PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?==[^\s\\]+")
_STATE_DIRECTORY = Path(".parity") / "workspace"
_WORKSPACE_EXTRA = "parity-check[workspace]"
_SOURCE_REPORT = "source-provenance.json"
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SOURCE_DIGEST = re.compile(r"^[0-9a-f]{64}$")

PackageMode = Literal["editable", "editable-legacy"]

_SOURCE_ORIGIN_PROBE = r"""
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

def normalized(value):
    return "-".join(filter(None, __import__("re").split(r"[-_.]+", value.lower())))

def contained(value, root):
    try:
        pathlib.Path(value).resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return False
    return True

try:
    subject = sys.argv[1]
    expected = pathlib.Path(sys.argv[2]).resolve(strict=True)
    names = set(json.loads(sys.argv[3]))
    distribution = importlib.metadata.distribution(subject)
    if normalized(distribution.metadata["Name"]) != subject:
        raise RuntimeError
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    parsed = urllib.parse.urlsplit(direct.get("url", ""))
    if parsed.scheme != "file" or not direct.get("dir_info", {}).get("editable"):
        raise RuntimeError
    raw_path = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw_path = "//" + parsed.netloc + raw_path
    if pathlib.Path(raw_path).resolve(strict=True) != expected:
        raise RuntimeError
    declared = distribution.read_text("top_level.txt") or ""
    for line in declared.splitlines():
        name = line.strip()
        if name.isidentifier():
            names.add(name)
    if not names or len(names) > 128:
        raise RuntimeError
    matched = False
    for name in sorted(names):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        if spec.origin not in {None, "built-in", "frozen"}:
            if not contained(spec.origin, expected):
                raise RuntimeError
            matched = True
            continue
        if spec.submodule_search_locations is not None and any(
            contained(location, expected) for location in spec.submodule_search_locations
        ):
            matched = True
            continue
        raise RuntimeError
    if not matched:
        raise RuntimeError
except BaseException:
    print("unverified")
    raise SystemExit(1)
print("verified")
"""


def _package_requirement(
    value: str, *, side: Literal["reference", "candidate"]
) -> tuple[Requirement, Specifier, tuple[str, ...]]:
    """Parse one intentionally narrow exact released-package contract."""

    try:
        requirement = Requirement(value)
    except InvalidRequirement as exc:
        raise ValueError(f"{side} package must be a valid PEP 508 requirement") from exc
    specifiers = list(requirement.specifier)
    if requirement.url is not None or requirement.marker is not None or len(specifiers) != 1:
        raise ValueError(f"{side} package must contain exactly one unconditional == version")
    specifier = specifiers[0]
    if specifier.operator != "==" or "*" in specifier.version:
        raise ValueError(f"{side} package must contain exactly one non-wildcard == version")
    try:
        Version(specifier.version)
    except InvalidVersion as exc:
        raise ValueError(f"{side} package == operand must be a valid PEP 440 version") from exc

    extras: tuple[str, ...] = ()
    opening = value.find("[")
    if opening >= 0:
        closing = value.find("]", opening)
        raw_extras = tuple(part.strip() for part in value[opening + 1 : closing].split(","))
        normalized = tuple(str(canonicalize_name(extra)) for extra in raw_extras)
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{side} package extras must be unique")
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

    version: Literal[3] = 3
    reference_package: str | None = None
    reference_path: Path | None = None
    candidate_package: str | None = None
    candidate_path: Path | None = None
    python: str | None = None
    reference_python: str | None = None
    candidate_python: str | None = None
    config: Path = Path("parity.toml")
    manifest: Path = Path("migration.toml")
    checklist: Path | None = None
    report_dir: Path = Path(".parity/workspace/reports")
    lanes: list[WorkspaceLane] = Field(
        default_factory=lambda: [WorkspaceLane(name="default")],
        min_length=1,
    )

    @field_validator("reference_package", "candidate_package")
    @classmethod
    def validate_package(cls, requirement: str | None, info: Any) -> str | None:
        if requirement is None:
            return None
        side: Literal["reference", "candidate"] = (
            "reference" if info.field_name == "reference_package" else "candidate"
        )
        try:
            _package_requirement(requirement, side=side)
        except ValueError as exc:
            raise ValueError(
                f"{side} package must be one exact requirement such as package==1.2.3 "
                f"or package[extra]==1.2.3 ({exc})"
            ) from exc
        return requirement

    @field_validator("python", "reference_python", "candidate_python")
    @classmethod
    def validate_python(cls, version: str | None) -> str | None:
        if version is None:
            return None
        match = _PYTHON_VERSION.fullmatch(version)
        if match is None:
            raise ValueError("python must be a major.minor version such as 3.12")
        if (int(match.group("major")), int(match.group("minor"))) < (3, 8):
            raise ValueError("target Python must be at least 3.8")
        return version

    @model_validator(mode="after")
    def validate_workspace(self) -> MigrationWorkspace:
        if (self.reference_package is None) == (self.reference_path is None):
            raise ValueError("set exactly one of reference_package or reference_path")
        if (self.candidate_package is None) == (self.candidate_path is None):
            raise ValueError("set exactly one of candidate_package or candidate_path")
        if self.python is None and (self.reference_python is None or self.candidate_python is None):
            raise ValueError("set shared python or both reference_python and candidate_python")
        names = [lane.name for lane in self.lanes]
        if len(names) != len(set(names)):
            raise ValueError("workspace lane names must be unique")
        return self

    @property
    def effective_reference_python(self) -> str:
        """Return the reference target version after applying the shared shorthand."""

        version = self.reference_python or self.python
        assert version is not None  # validated above
        return version

    @property
    def effective_candidate_python(self) -> str:
        """Return the candidate target version after applying the shared shorthand."""

        version = self.candidate_python or self.python
        assert version is not None  # validated above
        return version

    @property
    def reference_extras(self) -> tuple[str, ...]:
        """Return extras requested from both package implementations."""

        if self.reference_package is None:
            return ()
        _, _, extras = _package_requirement(self.reference_package, side="reference")
        return extras

    @property
    def candidate_extras(self) -> tuple[str, ...]:
        """Return extras requested from a released candidate package."""

        if self.candidate_package is None:
            return ()
        _, _, extras = _package_requirement(self.candidate_package, side="candidate")
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
    reference_package: str | None
    reference_path: Path | None
    reference_install_mode: PackageMode | None
    reference_extras: tuple[str, ...]
    candidate_package: str | None
    candidate_path: Path | None
    candidate_install_mode: PackageMode | None
    candidate_extras: tuple[str, ...]
    reference_python: str
    candidate_python: str
    config: Path
    manifest: Path
    report_dir: Path
    lanes: tuple[ResolvedWorkspaceLane, ...]
    checklist: Path | None = None

    @property
    def is_local_comparison(self) -> bool:
        """Whether both sides are user-owned source checkouts."""

        return self.reference_path is not None and self.candidate_path is not None

    @property
    def has_local_sources(self) -> bool:
        """Whether either side is an editable local checkout."""

        return self.reference_path is not None or self.candidate_path is not None

    @property
    def subject_name(self) -> str:
        """Return the normalized distribution under comparison."""

        if self.reference_package is not None:
            return _package_name(self.reference_package, side="reference")
        if self.reference_path is None:  # pragma: no cover - dataclass invariant
            raise WorkspaceError("workspace has no reference source")
        return _source_distribution_name(self.reference_path, side="reference")


class SourceRevision(StrictModel):
    """Path-free identity of one immutable source snapshot."""

    git_head: str
    dirty: bool
    source_sha256: str

    @field_validator("git_head")
    @classmethod
    def validate_git_head(cls, value: str) -> str:
        if _GIT_OBJECT_ID.fullmatch(value) is None:
            raise ValueError("git_head must be a lowercase Git object ID")
        return value

    @field_validator("source_sha256")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        if _SOURCE_DIGEST.fullmatch(value) is None:
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        return value


class WorkspaceSourceProvenance(StrictModel):
    """Data-safe source identity for one local/local comparison."""

    distribution: str
    reference: SourceRevision
    candidate: SourceRevision

    @field_validator("distribution")
    @classmethod
    def validate_distribution(cls, value: str) -> str:
        normalized = str(canonicalize_name(value))
        if value != normalized:
            raise ValueError("distribution must be normalized")
        return value


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
    source_provenance: Path | None = None


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
        "# Parity uses local checkouts in place; it never clones or modifies them.",
        "version = 3",
    ]
    if workspace.reference_package is not None:
        lines.append(f"reference_package = {_toml_string(workspace.reference_package)}")
    else:
        assert workspace.reference_path is not None
        lines.append(f"reference_path = {_toml_string(_path_text(workspace.reference_path))}")
    if workspace.candidate_package is not None:
        lines.append(f"candidate_package = {_toml_string(workspace.candidate_package)}")
    else:
        assert workspace.candidate_path is not None
        lines.append(f"candidate_path = {_toml_string(_path_text(workspace.candidate_path))}")
    if workspace.python is not None:
        lines.append(f"python = {_toml_string(workspace.python)}")
    if workspace.reference_python is not None:
        lines.append(f"reference_python = {_toml_string(workspace.reference_python)}")
    if workspace.candidate_python is not None:
        lines.append(f"candidate_python = {_toml_string(workspace.candidate_python)}")
    lines.extend(
        [
            f"config = {_toml_string(_path_text(workspace.config))}",
            f"manifest = {_toml_string(_path_text(workspace.manifest))}",
        ]
    )
    if workspace.checklist is not None:
        lines.append(f"checklist = {_toml_string(_path_text(workspace.checklist))}")
    lines.extend(
        [
            f"report_dir = {_toml_string(_path_text(workspace.report_dir))}",
            "",
        ]
    )
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


def rebase_workspace_path(
    value: str | Path,
    *,
    workspace_path: str | Path,
    invocation_cwd: str | Path,
) -> Path:
    """Express one invocation-relative source path beside a workspace document.

    CLI paths are conventionally interpreted from the directory in which the
    command was invoked, while workspace paths are deliberately interpreted
    beside ``parity.workspace.toml``.  This helper bridges those two contracts
    without requiring the process-wide current directory to match either one.
    """

    source = Path(value)
    invocation = Path(invocation_cwd).resolve()
    workspace = Path(workspace_path)
    if not workspace.is_absolute():
        workspace = invocation / workspace
    absolute = source.resolve() if source.is_absolute() else (invocation / source).resolve()
    return Path(os.path.relpath(absolute, workspace.resolve().parent))


def _resolve_report_dir(root: Path, value: Path) -> Path:
    """Resolve and require one dedicated report directory below the workspace root."""

    resolved_root = root.resolve()
    report_dir = (resolved_root / value).resolve() if not value.is_absolute() else value.resolve()
    _path_text(report_dir)
    if report_dir == resolved_root:
        raise WorkspaceError("workspace report_dir must be a dedicated contained subdirectory")
    try:
        report_dir.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceError("workspace report_dir must stay inside the workspace project") from exc
    return report_dir


def write_workspace(
    destination: str | Path = "parity.workspace.toml",
    *,
    reference_package: str | None = None,
    reference_path: Path | None = None,
    candidate_package: str | None = None,
    candidate_path: Path | None = None,
    python_version: str | None = None,
    reference_python_version: str | None = None,
    candidate_python_version: str | None = None,
    config: Path = Path("parity.toml"),
    manifest: Path = Path("migration.toml"),
    checklist: Path | None = None,
    report_dir: Path = Path(".parity/workspace/reports"),
    lanes: Sequence[WorkspaceLane] = (),
    force: bool = False,
    invocation_cwd: str | Path | None = None,
) -> Path:
    """Validate and atomically create one migration workspace document."""

    path = Path(destination)
    if candidate_path is None and candidate_package is None:
        candidate_path = Path(".")
    if invocation_cwd is not None:
        invocation = Path(invocation_cwd).resolve()
        if not path.is_absolute():
            path = invocation / path
        if candidate_path is not None:
            candidate_path = rebase_workspace_path(
                candidate_path,
                workspace_path=path,
                invocation_cwd=invocation,
            )
        if reference_path is not None:
            reference_path = rebase_workspace_path(
                reference_path,
                workspace_path=path,
                invocation_cwd=invocation,
            )
        config = rebase_workspace_path(
            config,
            workspace_path=path,
            invocation_cwd=invocation,
        )
        manifest = rebase_workspace_path(
            manifest,
            workspace_path=path,
            invocation_cwd=invocation,
        )
        if checklist is not None:
            checklist = rebase_workspace_path(
                checklist,
                workspace_path=path,
                invocation_cwd=invocation,
            )
        # Generated reports belong to the workspace root.  Unlike source and
        # resolver inputs, their default is not an invocation-relative input.
        effective_lanes = [
            WorkspaceLane(
                name=lane.name,
                requirements=(
                    rebase_workspace_path(
                        lane.requirements,
                        workspace_path=path,
                        invocation_cwd=invocation,
                    )
                    if lane.requirements is not None
                    else None
                ),
            )
            for lane in lanes
        ]
    else:
        effective_lanes = list(lanes)
    effective_lanes = effective_lanes or [WorkspaceLane(name="default")]
    try:
        if (
            python_version is None
            and reference_python_version is None
            and candidate_python_version is None
        ):
            python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        workspace = MigrationWorkspace(
            reference_package=reference_package,
            reference_path=reference_path,
            candidate_package=candidate_package,
            candidate_path=candidate_path,
            python=python_version,
            reference_python=reference_python_version,
            candidate_python=candidate_python_version,
            config=config,
            manifest=manifest,
            checklist=checklist,
            report_dir=report_dir,
            lanes=effective_lanes,
        )
    except ValueError as exc:
        raise WorkspaceError(f"invalid migration workspace: {exc}") from exc
    root = path.resolve().parent
    _resolve_report_dir(root, workspace.report_dir)
    _validate_config_replay_base(root, _resolve_path(root, workspace.config))
    if workspace.checklist is not None:
        _resolve_checklist(root, workspace.checklist)
    _atomic_write_text(path, render_workspace(workspace), force=force)
    return path


def _resolve_path(root: Path, value: Path) -> Path:
    path = (root / value).resolve() if not value.is_absolute() else value.resolve()
    _path_text(path)
    return path


def _resolve_checklist(root: Path, value: Path) -> Path:
    """Resolve and contain the agent-review checklist within the workspace project."""

    checklist = _resolve_path(root, value)
    try:
        checklist.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkspaceError("workspace checklist must stay inside the workspace project") from exc
    return checklist


def _validate_config_replay_base(root: Path, config: Path) -> None:
    """Require the config directory to contain every managed runtime path."""

    try:
        root.resolve().relative_to(config.resolve().parent)
    except ValueError as exc:
        raise WorkspaceError(
            "the directory holding the workspace config must contain the workspace directory; "
            "this keeps managed targets, environments and replay paths project-local"
        ) from exc


def _package_name(requirement: str, *, side: Literal["reference", "candidate"]) -> str:
    parsed, _, _ = _package_requirement(requirement, side=side)
    return str(canonicalize_name(parsed.name))


def _package_contract(
    requirement: str, *, side: Literal["reference", "candidate"]
) -> tuple[str, str, Version]:
    """Return a normalized subject name and exact released-version contract."""

    parsed, specifier, _ = _package_requirement(requirement, side=side)
    version = Version(specifier.version)
    return str(canonicalize_name(parsed.name)), str(specifier), version


def _source_distribution_name(source: Path, *, side: str) -> str:
    """Read a source distribution name without executing project code."""

    declared: list[str] = []
    pyproject = source / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise WorkspaceError(f"{side} pyproject.toml could not be read safely") from exc
        project = document.get("project")
        if isinstance(project, dict) and isinstance(project.get("name"), str):
            declared.append(project["name"])
        tool = document.get("tool")
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
                declared.append(poetry["name"])

    setup_cfg = source / "setup.cfg"
    if setup_cfg.is_file():
        parser = ConfigParser(interpolation=None)
        try:
            with setup_cfg.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (OSError, UnicodeError, ConfigParserError) as exc:
            raise WorkspaceError(f"{side} setup.cfg could not be read safely") from exc
        if parser.has_option("metadata", "name"):
            declared.append(parser.get("metadata", "name"))

    normalized = {str(canonicalize_name(name)) for name in declared if name.strip()}
    if len(normalized) != 1:
        detail = "is not declared statically" if not normalized else "is contradictory"
        raise WorkspaceError(
            f"{side} distribution name {detail}; managed setup requires project.name, "
            "tool.poetry.name or setup.cfg [metadata] name so it cannot install the wrong package. "
            "Dynamic setup.py metadata is intentionally not executed by the Parity driver"
        )
    return normalized.pop()


def _source_package_mode(source: Path, *, side: str) -> PackageMode:
    """Validate one local package source and select its tox package mode."""

    if not source.is_dir():
        raise WorkspaceError(f"{side} must be an existing local checkout directory")
    if (source / "pyproject.toml").is_file():
        return "editable"
    if (source / "setup.py").is_file():
        return "editable-legacy"
    raise WorkspaceError(
        f"{side} checkout needs pyproject.toml or setup.py; "
        "Parity does not fetch or modify source code"
    )


def _load_workspace_document(path: str | Path) -> tuple[Path, MigrationWorkspace]:
    """Load the strict human-authored document without resolving its sources."""

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
    return workspace_path, document


def load_workspace(path: str | Path = "parity.workspace.toml") -> ResolvedWorkspace:
    """Load a workspace and validate every setup-time source input."""

    workspace_path, document = _load_workspace_document(path)

    root = workspace_path.parent
    candidate_path = (
        _resolve_path(root, document.candidate_path)
        if document.candidate_path is not None
        else None
    )
    candidate_install_mode = (
        _source_package_mode(candidate_path, side="candidate")
        if candidate_path is not None
        else None
    )
    if candidate_path is not None:
        candidate_name = _source_distribution_name(candidate_path, side="candidate")
    else:
        assert document.candidate_package is not None
        candidate_name = _package_name(document.candidate_package, side="candidate")
    reference_path = (
        _resolve_path(root, document.reference_path)
        if document.reference_path is not None
        else None
    )
    reference_install_mode = (
        _source_package_mode(reference_path, side="reference")
        if reference_path is not None
        else None
    )
    if reference_path is not None:
        if candidate_path is not None and reference_path == candidate_path:
            raise WorkspaceError("reference_path and candidate_path must be different checkouts")
        reference_name = _source_distribution_name(reference_path, side="reference")
    else:
        assert document.reference_package is not None
        reference_name = _package_name(document.reference_package, side="reference")
    if candidate_name != reference_name:
        raise WorkspaceError(
            f"candidate distribution {candidate_name!r} does not match reference distribution "
            f"{reference_name!r}; refusing to compare different subjects"
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
    checklist = (
        _resolve_checklist(root, document.checklist) if document.checklist is not None else None
    )
    _validate_config_replay_base(root, config)
    if not config.is_file():
        raise WorkspaceError(
            "the workspace Parity config is missing; migration init expects an existing parity.toml"
        )
    if not manifest.is_file():
        raise WorkspaceError(
            "the workspace migration ledger is missing; migration init expects an existing "
            "migration.toml"
        )
    if checklist is not None and not checklist.is_file():
        raise WorkspaceError("the workspace contract checklist is missing")

    report_dir = _resolve_report_dir(root, document.report_dir)

    return ResolvedWorkspace(
        path=workspace_path,
        root=root,
        reference_package=document.reference_package,
        reference_path=reference_path,
        reference_install_mode=reference_install_mode,
        reference_extras=document.reference_extras,
        candidate_package=document.candidate_package,
        candidate_path=candidate_path,
        candidate_install_mode=candidate_install_mode,
        candidate_extras=document.candidate_extras,
        reference_python=document.effective_reference_python,
        candidate_python=document.effective_candidate_python,
        config=config,
        manifest=manifest,
        checklist=checklist,
        report_dir=report_dir,
        lanes=tuple(resolved_lanes),
    )


def _existing_report_directory(workspace: ResolvedWorkspace) -> Path | None:
    """Return a safe existing report directory without creating new state."""

    if not os.path.lexists(workspace.report_dir):
        return None
    resolved = workspace.report_dir.resolve()
    try:
        resolved.relative_to(workspace.root)
    except ValueError as exc:
        raise WorkspaceError("workspace report_dir resolves outside the project") from exc
    if not resolved.is_dir():
        raise WorkspaceError("workspace report_dir is not a directory")
    return resolved


def _invalidate_lane_report(report_dir: Path, lane_name: str) -> None:
    """Remove one fixed active report so a failed run cannot leave stale success."""

    if re.fullmatch(_WORKSPACE_NAME_PATTERN, lane_name) is None:  # pragma: no cover
        raise WorkspaceError("invalid migration workspace lane name")
    report = report_dir / f"{lane_name}.json"
    if not os.path.lexists(report):
        return
    if not (report.is_file() or report.is_symlink()):
        raise WorkspaceError("active migration report is not a replaceable file")
    try:
        report.unlink()
    except OSError as exc:
        raise WorkspaceError("active migration report could not be invalidated") from exc


def _is_generated_lane_report(path: Path) -> bool:
    """Recognize the exact top-level shape written by ``write_migration_json``."""

    if path.is_symlink():
        return False
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and set(payload)
        == {"schema_version", "status", "summary", "units", "manifest_sha256", "parity"}
        and type(payload.get("schema_version")) is int
        and payload["schema_version"] == 1
    )


def _is_generated_source_report(path: Path) -> bool:
    """Recognize a complete path-free source provenance report."""

    if path.is_symlink():
        return False
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False
    try:
        WorkspaceSourceProvenance.model_validate(
            {key: value for key, value in payload.items() if key != "schema_version"}
        )
    except ValueError:
        return False
    return set(payload) == {"schema_version", "distribution", "reference", "candidate"}


def _invalidate_active_reports(report_dir: Path, lane_names: Sequence[str]) -> None:
    """Discard generated lane reports while preserving unrelated files."""

    try:
        entries = tuple(report_dir.iterdir())
    except OSError as exc:
        raise WorkspaceError("active migration reports could not be inspected") from exc
    active_names = set(lane_names)
    for entry in entries:
        if entry.name == _SOURCE_REPORT:
            if not entry.is_symlink() and not _is_generated_source_report(entry):
                continue
            if not (entry.is_file() or entry.is_symlink()):
                raise WorkspaceError("active source provenance report is not a replaceable file")
            try:
                entry.unlink()
            except OSError as exc:
                raise WorkspaceError(
                    "active source provenance report could not be invalidated"
                ) from exc
            continue
        if entry.suffix != ".json" or re.fullmatch(_WORKSPACE_NAME_PATTERN, entry.stem) is None:
            continue
        if entry.stem not in active_names and not _is_generated_lane_report(entry):
            continue
        _invalidate_lane_report(report_dir, entry.stem)


def _invalidate_declared_workspace_reports(path: str | Path) -> None:
    """Invalidate active evidence before any environment or project validation."""

    workspace_path, document = _load_workspace_document(path)
    report_dir = _resolve_report_dir(workspace_path.parent, document.report_dir)
    if not os.path.lexists(report_dir):
        return
    if not report_dir.is_dir():
        raise WorkspaceError("workspace report_dir is not a directory")
    _invalidate_active_reports(report_dir, [lane.name for lane in document.lanes])


def _git_output(source: Path, arguments: Sequence[str]) -> bytes:
    """Run one read-only Git query without exposing source paths in failures."""

    git = shutil.which("git")
    if git is None:
        raise WorkspaceError("local source comparison requires Git on PATH")
    try:
        completed = subprocess.run(
            [git, "-C", str(source), *arguments],
            cwd=source,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise WorkspaceError("local source Git inspection could not start") from exc
    if completed.returncode != 0:
        raise WorkspaceError(
            "local sources must be Git worktrees with a committed HEAD; "
            "Parity never creates or changes worktrees"
        )
    return completed.stdout


def _git_root(source: Path) -> Path:
    raw = _git_output(source, ["rev-parse", "--show-toplevel"])
    try:
        root = Path(os.fsdecode(raw.rstrip(b"\r\n"))).resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("Git reported an invalid local source worktree") from exc
    if not root.is_dir():  # pragma: no cover - resolve(strict=True) narrows this in practice
        raise WorkspaceError("Git reported an invalid local source worktree")
    try:
        source.resolve().relative_to(root)
    except ValueError as exc:
        raise WorkspaceError("local source is not contained by its Git worktree") from exc
    return root


def _hash_source_entry(digest: Any, root: Path, relative: Path) -> None:
    """Hash one lexical Git source entry without following symlink targets."""

    path = root / relative
    encoded_path = os.fsencode(relative.as_posix())
    digest.update(len(encoded_path).to_bytes(8, "big"))
    digest.update(encoded_path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise WorkspaceError("a local source entry could not be inspected") from exc

    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        digest.update(b"symlink\0")
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise WorkspaceError("a local source symlink could not be inspected") from exc
        target_bytes = os.fsencode(target)
        digest.update(len(target_bytes).to_bytes(8, "big"))
        digest.update(target_bytes)
        return
    if stat.S_ISREG(mode):
        digest.update(b"file\0")
        digest.update(b"executable\0" if mode & 0o111 else b"regular\0")
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise WorkspaceError("a local source file could not be read") from exc
        return
    if stat.S_ISDIR(mode):
        # Git links (submodules) appear as directory entries. Their checked-out
        # contents and nested HEAD/status must participate in the snapshot.
        digest.update(b"directory\0")
        if _git_root(path) == root:
            raise WorkspaceError(
                "local source contains an uninitialized Git submodule; "
                "initialize it before running Parity"
            )
        nested = _source_revision(path)
        digest.update(nested.git_head.encode("ascii"))
        digest.update(b"dirty\0" if nested.dirty else b"clean\0")
        digest.update(nested.source_sha256.encode("ascii"))
        return
    raise WorkspaceError("local source contains an unsupported filesystem entry")


def _source_revision(source: Path) -> SourceRevision:
    """Capture one deterministic, path-free Git worktree snapshot."""

    root = _git_root(source)
    head_bytes = _git_output(root, ["rev-parse", "--verify", "HEAD"]).strip()
    try:
        head = head_bytes.decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover - Git object IDs are ASCII
        raise WorkspaceError("Git returned an invalid local source HEAD") from exc
    if _GIT_OBJECT_ID.fullmatch(head) is None:
        raise WorkspaceError("Git returned an invalid local source HEAD")

    status_before = _git_output(
        root,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
    )

    def tree_digest() -> str:
        listed = _git_output(
            root,
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        )
        raw_paths = [item for item in listed.split(b"\0") if item]
        if len(raw_paths) != len(set(raw_paths)):
            raise WorkspaceError("Git returned duplicate local source entries")
        digest = hashlib.sha256(b"parity-source-v1\0")
        for raw_path in sorted(raw_paths):
            relative = Path(os.fsdecode(raw_path))
            if relative.is_absolute() or ".." in relative.parts:
                raise WorkspaceError("Git returned an unsafe local source entry")
            _hash_source_entry(digest, root, relative)
        return digest.hexdigest()

    source_digest = tree_digest()
    head_after = _git_output(root, ["rev-parse", "--verify", "HEAD"]).strip()
    status_after = _git_output(
        root,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
    )
    if head_after != head_bytes or status_after != status_before or tree_digest() != source_digest:
        raise WorkspaceError("a local source changed while Parity was inspecting it")
    return SourceRevision(
        git_head=head,
        dirty=bool(status_before),
        source_sha256=source_digest,
    )


def _capture_source_provenance(workspace: ResolvedWorkspace) -> WorkspaceSourceProvenance | None:
    """Capture both user-owned sources for a local-to-local comparison."""

    if not workspace.is_local_comparison:
        return None
    assert workspace.reference_path is not None
    assert workspace.candidate_path is not None
    return WorkspaceSourceProvenance(
        distribution=workspace.subject_name,
        reference=_source_revision(workspace.reference_path),
        candidate=_source_revision(workspace.candidate_path),
    )


def _assert_sources_unchanged(
    workspace: ResolvedWorkspace,
    expected: WorkspaceSourceProvenance | None,
) -> None:
    if expected is None:
        return
    if _capture_source_provenance(workspace) != expected:
        raise WorkspaceError(
            "a local source changed during managed execution; results are not valid"
        )


def _runtime_source_revision(runtime: object, subject: str) -> SourceRevision | None:
    """Extract one portable endpoint identity without retaining a source path."""

    identities = getattr(runtime, "identities", ())
    for identity in identities:
        if getattr(identity, "name", None) != subject:
            continue
        if getattr(identity, "kind", None) != "git-worktree-v1":
            raise WorkspaceError("target reported an unsupported local source identity")
        try:
            return SourceRevision(
                git_head=identity.revision,
                dirty=identity.dirty,
                source_sha256=identity.sha256,
            )
        except (AttributeError, ValueError) as exc:
            raise WorkspaceError("target reported an invalid local source identity") from exc
    return None


def _assert_lane_source_provenance(
    result: MigrationResult,
    expected: WorkspaceSourceProvenance | None,
) -> None:
    """Require semantic lane evidence to carry each driver's source snapshot."""

    if expected is None:
        return
    for case in result.suite.cases:
        if case.status in {Status.ERROR, Status.SKIPPED}:
            continue
        if case.provenance is None:
            raise WorkspaceError("target did not report local source provenance")
        for side, expected_revision in (
            ("reference", expected.reference),
            ("candidate", expected.candidate),
        ):
            runtime = getattr(case.provenance, side)
            observed = (
                _runtime_source_revision(runtime, expected.distribution)
                if runtime is not None
                else None
            )
            if observed != expected_revision:
                raise WorkspaceError(
                    f"{side} target source provenance did not match the inspected checkout"
                )


def _write_source_provenance(
    provenance: WorkspaceSourceProvenance,
    destination: Path,
) -> Path:
    """Write a path-free, relocatable source identity report atomically."""

    payload = {
        "schema_version": 1,
        **provenance.model_dump(mode="json"),
    }
    if os.path.lexists(destination) and not (
        destination.is_symlink() or _is_generated_source_report(destination)
    ):
        raise WorkspaceError(
            "source provenance destination contains an unrelated file; choose another report_dir"
        )
    _atomic_write_text(
        destination,
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        force=True,
    )
    return destination


def advance_workspace(
    path: str | Path = "parity.workspace.toml",
    *,
    reference_package: str,
) -> Path:
    """Atomically promote a released baseline while preserving the active harness.

    The workspace remains a description of one adjacent pair.  Lanes, source
    paths and compatible locks are retained; only disposable active lane reports
    are invalidated.  Advancing to a different distribution is rejected.
    """

    workspace_path, document = _load_workspace_document(path)
    if document.reference_package is None:
        raise WorkspaceError(
            "migration advance applies only to released references; "
            "update reference_path explicitly for a local checkout pair"
        )
    try:
        advanced = document.model_copy(update={"reference_package": reference_package})
        # ``model_copy(update=...)`` intentionally does not validate updates.
        advanced = MigrationWorkspace.model_validate(advanced.model_dump(mode="python"))
    except ValueError as exc:
        raise WorkspaceError(f"invalid migration workspace: {exc}") from exc
    assert advanced.reference_package is not None
    current_name = _package_name(document.reference_package, side="reference")
    advanced_name = _package_name(advanced.reference_package, side="reference")
    if advanced_name != current_name:
        raise WorkspaceError("an adjacent migration cannot change the subject distribution")
    _, _, current_version = _package_contract(document.reference_package, side="reference")
    _, _, advanced_version = _package_contract(advanced.reference_package, side="reference")
    if advanced_version <= current_version:
        raise WorkspaceError("the advanced reference version must be newer than the active one")

    resolved = load_workspace(workspace_path)
    if report_dir := _existing_report_directory(resolved):
        _invalidate_active_reports(report_dir, [lane.name for lane in resolved.lanes])
    _atomic_write_text(workspace_path, render_workspace(advanced), force=True)
    return workspace_path


def _state_root(workspace: ResolvedWorkspace) -> Path:
    """Create the private state root, rejecting redirects outside the project."""

    project_root = workspace.root.resolve()
    private_root = project_root / _STATE_DIRECTORY.parent
    if os.path.lexists(private_root):
        if private_root.is_symlink():
            raise WorkspaceError("private migration workspace root cannot be a symbolic link")
        if not private_root.is_dir():
            raise WorkspaceError("private migration workspace root is not a directory")
    try:
        private_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError("could not create private migration workspace state") from exc
    resolved_private_root = private_root.resolve()
    try:
        resolved_private_root.relative_to(project_root)
    except ValueError as exc:
        raise WorkspaceError(
            "private migration workspace root resolves outside the project"
        ) from exc
    if not resolved_private_root.is_dir():
        raise WorkspaceError("private migration workspace root is not a directory")

    state = resolved_private_root / _STATE_DIRECTORY.name
    try:
        if os.path.lexists(state) and state.is_symlink():
            raise WorkspaceError("private migration workspace state cannot be a symbolic link")
        state.mkdir(parents=False, exist_ok=True)
    except WorkspaceError:
        raise
    except OSError as exc:
        raise WorkspaceError("could not create private migration workspace state") from exc
    if state.is_symlink():
        raise WorkspaceError("private migration workspace state cannot be a symbolic link")
    resolved = state.resolve()
    try:
        resolved.relative_to(resolved_private_root)
    except ValueError as exc:
        raise WorkspaceError(
            "private migration workspace state resolves outside the project"
        ) from exc
    if not resolved.is_dir():
        raise WorkspaceError("private migration workspace state is not a directory")
    ignore = resolved_private_root / ".gitignore"
    if not os.path.lexists(ignore):
        try:
            _atomic_write_text(ignore, "*\n", force=False)
        except FileExistsError:
            # A concurrent creator owns the policy; never replace it.
            pass
        except OSError as exc:
            raise WorkspaceError("could not protect private migration workspace state") from exc
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
        "",
    ]
    for lane in workspace.lanes:
        reference_env = _lane_env_name(lane.name, "reference")
        candidate_env = _lane_env_name(lane.name, "candidate")
        common = [
            'runner = "uv-venv-runner"',
            "pass_env = []",
            "commands = []",
        ]
        reference_lock = state_root / "locks" / f"requirements.{lane.name}.reference.txt"
        candidate_lock = state_root / "locks" / f"requirements.{lane.name}.candidate.txt"
        reference_mode = workspace.reference_install_mode or "skip"
        reference_section = [
            f"[env.{_toml_string(reference_env)}]",
            'description = "locked reference worker"',
            f"package = {_toml_string(reference_mode)}",
            f"base_python = [{_toml_string(workspace.reference_python)}]",
        ]
        if workspace.reference_path is not None:
            reference_section.append(
                f"package_root = {_toml_string(_path_text(workspace.reference_path))}",
            )
        reference_section.extend(
            [
                f"deps = [{_toml_string(f'-r{_path_text(reference_lock)}')}]",
                *common,
            ]
        )
        if workspace.reference_path is not None:
            reference_section.extend(
                [
                    "constrain_package_deps = true",
                    "use_frozen_constraints = true",
                ]
            )
        if workspace.reference_path is not None and workspace.candidate_extras:
            reference_section.append(f"extras = {_toml_array(workspace.candidate_extras)}")
        if workspace.reference_install_mode == "editable-legacy":
            reference_section.append("uv_seed = true")

        candidate_section = [
            f"[env.{_toml_string(candidate_env)}]",
            'description = "locked candidate worker"',
            f"package = {_toml_string(workspace.candidate_install_mode or 'skip')}",
            f"base_python = [{_toml_string(workspace.candidate_python)}]",
            f"deps = [{_toml_string(f'-r{_path_text(candidate_lock)}')}]",
            *common,
        ]
        if workspace.candidate_path is not None:
            candidate_section.extend(
                [
                    f"package_root = {_toml_string(_path_text(workspace.candidate_path))}",
                    "constrain_package_deps = true",
                    "use_frozen_constraints = true",
                ]
            )
        if workspace.candidate_path is not None and workspace.reference_extras:
            # A released reference requirement already carries the extras. The
            # candidate editable needs the matching optional dependency set.
            candidate_section.append(f"extras = {_toml_array(workspace.reference_extras)}")
        if workspace.candidate_install_mode == "editable-legacy":
            candidate_section.append("uv_seed = true")
        lines.extend(
            [
                *reference_section,
                "",
                *candidate_section,
                "",
            ]
        )
    return "\n".join(lines)


ToolCommand = str | Sequence[str]


def _tool(name: str) -> tuple[str, ...]:
    """Run controller tools through this interpreter, never through ``PATH``."""

    try:
        installed = importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        installed = False
    if not installed:
        raise WorkspaceError(
            f"migration environment support is not installed ({name} is missing); "
            f"install {_WORKSPACE_EXTRA!r}"
        )
    # Isolated mode removes the workspace from module discovery, so a local
    # ``uv.py`` or ``tox.py`` cannot shadow the controller's installed tool.
    return (sys.executable, "-I", "-m", name)


def _tool_prefix(command: ToolCommand) -> list[str]:
    """Normalize legacy test doubles and the module-based runtime command."""

    return [command] if isinstance(command, str) else list(command)


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


def _package_dependency_input(source: Path) -> Path:
    """Return packaging metadata that uv can resolve without installing the project."""

    for name in ("pyproject.toml", "setup.py", "setup.cfg"):
        candidate = source / name
        if candidate.is_file():
            return candidate
    raise WorkspaceError("local package metadata disappeared during setup")


def _worker_runtime_requirements() -> tuple[str, ...]:
    """Return only dependencies required by the portable Arrow transport."""

    return ("pyarrow>=16",)


def _compile_lane_lock(
    workspace: ResolvedWorkspace,
    lane: ResolvedWorkspaceLane,
    *,
    side: Literal["reference", "candidate"],
    uv: ToolCommand,
    state_root: Path,
    refresh: bool,
) -> Path:
    inputs_dir = _private_state_directory(state_root, "inputs")
    locks_dir = _private_state_directory(state_root, "locks")
    generated_input = inputs_dir / f"{lane.name}.{side}.in"
    source_requirement = ""
    package_input: Path | None = None
    if side == "reference":
        if workspace.reference_package is not None:
            source_requirement = f"{workspace.reference_package}\n"
        else:
            assert workspace.reference_path is not None
            package_input = _package_dependency_input(workspace.reference_path)
    else:
        if workspace.candidate_package is not None:
            source_requirement = f"{workspace.candidate_package}\n"
        else:
            assert workspace.candidate_path is not None
            package_input = _package_dependency_input(workspace.candidate_path)
    _atomic_write_text(
        generated_input,
        "# Generated by Parity. Do not edit.\n"
        + "".join(f"{requirement}\n" for requirement in _worker_runtime_requirements())
        + source_requirement,
        force=True,
    )

    lock = locks_dir / f"requirements.{lane.name}.{side}.txt"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"requirements.{lane.name}.{side}.", suffix=".txt", dir=locks_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        if lock.is_file() and not refresh:
            shutil.copyfile(lock, temporary)
        command = [
            *_tool_prefix(uv),
            "pip",
            "compile",
            "--format",
            "requirements-txt",
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--python-version",
            (workspace.reference_python if side == "reference" else workspace.candidate_python),
            "--output-file",
            str(temporary),
        ]
        if refresh:
            command.append("--upgrade")
        local_extras = (
            workspace.candidate_extras if side == "reference" else workspace.reference_extras
        )
        if local_extras and package_input is not None:
            for extra in local_extras:
                command.extend(["--extra", extra])
        if lane.requirements is not None:
            command.append(str(lane.requirements))
        command.append(str(generated_input))
        if package_input is not None:
            command.append(str(package_input))
        _run_checked(
            command,
            cwd=workspace.root,
            operation=f"dependency resolution for lane {lane.name!r} {side} worker",
            failure_log=_failure_log(state_root, f"resolve-{lane.name}-{side}"),
        )
        try:
            rendered_lock = temporary.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkspaceError(
                f"dependency resolution for lane {lane.name!r} {side} worker did not produce "
                "a valid lock"
            ) from exc
        if not any(_PINNED_REQUIREMENT.match(line) for line in rendered_lock.splitlines()):
            raise WorkspaceError(
                f"dependency resolution for lane {lane.name!r} {side} worker produced no "
                "pinned requirements"
            )
        os.replace(temporary, lock)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return lock


def _tox_base(
    tox: ToolCommand, workspace: ResolvedWorkspace, tox_config: Path, state_root: Path
) -> list[str]:
    return [
        *_tool_prefix(tox),
        "--colored",
        "no",
        "-c",
        str(tox_config),
        "--root",
        str(workspace.root),
        "--workdir",
        str(_private_state_directory(state_root, "envs")),
    ]


def _query_env_python(
    tox: ToolCommand,
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


def _setup_resolved_workspace(
    workspace: ResolvedWorkspace,
    *,
    refresh_locks: bool = False,
) -> WorkspaceSetup:
    """Provision one already validated workspace without reloading its contract."""

    uv = _tool("uv")
    tox = _tool("tox")
    state_root = _state_root(workspace)
    for child in ("inputs", "locks", "envs", "logs"):
        _private_state_directory(state_root, child)
    for lane in workspace.lanes:
        for side in ("reference", "candidate"):
            _compile_lane_lock(
                workspace,
                lane,
                side=side,
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


def setup_workspace(
    path: str | Path = "parity.workspace.toml",
    *,
    refresh_locks: bool = False,
) -> WorkspaceSetup:
    """Resolve locks, prepare every tox environment and return worker paths."""

    workspace = load_workspace(path)
    source_provenance = _capture_source_provenance(workspace)
    config = _bind_subject_distribution(workspace, load_config(workspace.config))
    _validate_source_import_isolation(workspace, config)
    setup = _setup_resolved_workspace(workspace, refresh_locks=refresh_locks)
    _validate_local_source_installs(setup, config)
    _assert_sources_unchanged(workspace, source_provenance)
    return setup


def _bind_subject_distribution(
    workspace: ResolvedWorkspace,
    config: ParityConfig,
) -> ParityConfig:
    """Bind the managed workspace's subject into every effective worker contract."""

    subject = workspace.subject_name
    effective = config.model_copy(deep=True)
    for case in effective.cases:
        if workspace.reference_package is not None:
            _, exact_requirement, reference_version = _package_contract(
                workspace.reference_package, side="reference"
            )
            existing = case.reference.required_distributions.get(subject)
            if existing is not None and reference_version not in SpecifierSet(existing):
                raise WorkspaceError(
                    "reference runtime requirements conflict with the workspace subject version"
                )
            case.reference.required_distributions = {
                **case.reference.required_distributions,
                subject: exact_requirement,
            }
        if workspace.candidate_package is not None:
            _, exact_requirement, candidate_version = _package_contract(
                workspace.candidate_package, side="candidate"
            )
            existing = case.candidate.required_distributions.get(subject)
            if existing is not None and candidate_version not in SpecifierSet(existing):
                raise WorkspaceError(
                    "candidate runtime requirements conflict with the workspace subject version"
                )
            case.candidate.required_distributions = {
                **case.candidate.required_distributions,
                subject: exact_requirement,
            }
        for endpoint in (case.reference, case.candidate):
            endpoint.record_distributions = sorted(
                set(endpoint.record_distributions).union({subject})
            )
            # Managed workers import wrappers from the neutral harness; local
            # subjects arrive only through their verified installations.
            endpoint.workdir = workspace.root
    return effective


def _pythonpath_entries(value: str, *, base: Path) -> tuple[Path, ...]:
    """Resolve the effective Python import roots using subprocess cwd semantics."""

    entries: list[Path] = []
    for raw in value.split(os.pathsep):
        # An empty PYTHONPATH component denotes the worker current directory.
        path = Path(raw) if raw else base
        if not path.is_absolute():
            path = base / path
        entries.append(path.resolve())
    return tuple(entries)


def _validate_source_import_isolation(
    workspace: ResolvedWorkspace,
    config: ParityConfig,
) -> None:
    """Reject layouts that can bypass either managed editable installation."""

    def exposes_source(import_root: Path, source: Path) -> bool:
        root = import_root.resolve()
        source = source.resolve()
        if root == source or root == (source / "src").resolve():
            return True
        try:
            source.relative_to(root)
        except ValueError:
            return False
        return True

    sources: list[tuple[str, Path]] = []
    if workspace.candidate_path is not None:
        sources.append(("candidate", workspace.candidate_path))
    if workspace.reference_path is not None:
        sources.append(("reference", workspace.reference_path))
    for label, source in sources:
        if exposes_source(workspace.root, source):
            raise WorkspaceError(
                f"workspace root exposes the {label} checkout to a managed worker; place the "
                "workspace and wrappers in a contained subdirectory such as migrations"
            )

    inherited = os.environ.get("PYTHONPATH", "")
    for case in config.cases:
        for side, endpoint in (("reference", case.reference), ("candidate", case.candidate)):
            effective_pythonpath = endpoint.environment.get("PYTHONPATH", inherited)
            for import_root in _pythonpath_entries(
                effective_pythonpath,
                base=workspace.root,
            ):
                for label, source in sources:
                    if not exposes_source(import_root, source):
                        continue
                    raise WorkspaceError(
                        f"{side} PYTHONPATH exposes the {label} checkout; remove that import root "
                        "before running the managed workspace"
                    )


_NON_PACKAGE_ROOTS = {
    "benchmarks",
    "build",
    "dist",
    "docs",
    "examples",
    "scripts",
    "test",
    "tests",
    "tools",
}


def _declared_python_modules(source: Path) -> set[str]:
    """Read explicit single-module declarations without executing packaging code."""

    modules: set[str] = set()
    pyproject = source / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise WorkspaceError("local pyproject.toml could not be read safely") from exc
        tool = document.get("tool")
        setuptools = tool.get("setuptools") if isinstance(tool, dict) else None
        declared = setuptools.get("py-modules") if isinstance(setuptools, dict) else None
        if isinstance(declared, list):
            modules.update(item for item in declared if isinstance(item, str))

    setup_cfg = source / "setup.cfg"
    if setup_cfg.is_file():
        parser = ConfigParser(interpolation=None)
        try:
            with setup_cfg.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (OSError, UnicodeError, ConfigParserError) as exc:
            raise WorkspaceError("local setup.cfg could not be read safely") from exc
        if parser.has_option("options", "py_modules"):
            modules.update(
                item.strip()
                for line in parser.get("options", "py_modules").splitlines()
                for item in line.split(",")
                if item.strip()
            )
    return {name for name in modules if name.isidentifier()}


def _contains_python_source(directory: Path) -> bool:
    """Find Python source below a namespace root without following symlink trees."""

    pending = [directory]
    inspected = 0
    while pending:
        current = pending.pop()
        try:
            entries = tuple(current.iterdir())
        except OSError as exc:
            raise WorkspaceError("local source import roots could not be inspected") from exc
        for entry in entries:
            inspected += 1
            if inspected > 10_000:
                raise WorkspaceError("local source import discovery exceeded its safety bound")
            if entry.is_symlink() or entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            if entry.is_file() and entry.suffix in {".py", ".pyi"}:
                return True
            if entry.is_dir():
                pending.append(entry)
    return False


def _source_import_names(source: Path, distribution: str) -> tuple[str, ...]:
    """Infer package imports while excluding flat-layout tooling and test trees."""

    import_root = source / "src" if (source / "src").is_dir() else source
    distribution_module = distribution.replace("-", "_")
    declared_modules = _declared_python_modules(source)
    names: set[str] = set()
    try:
        entries = tuple(import_root.iterdir())
    except OSError as exc:
        raise WorkspaceError("local source import roots could not be inspected") from exc
    for entry in entries:
        name: str | None = None
        if (
            entry.is_file()
            and entry.suffix in {".py", ".pyi"}
            and (entry.stem == distribution_module or entry.stem in declared_modules)
        ):
            name = entry.stem
        elif (
            entry.is_dir()
            and not entry.is_symlink()
            and not entry.name.startswith(".")
            and (entry.name not in _NON_PACKAGE_ROOTS or entry.name == distribution_module)
            and (
                (entry / "__init__.py").is_file()
                or (entry / "__init__.pyi").is_file()
                or _contains_python_source(entry)
            )
        ):
            name = entry.name
        if name is not None and name.isidentifier():
            names.add(name)
        if len(names) > 128:
            raise WorkspaceError("local source declares too many top-level import candidates")
    if not names:
        raise WorkspaceError("local source declares no importable Python packages")
    return tuple(sorted(names))


def _origin_probe_environment(overrides: dict[str, str]) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(overrides)
    return environment


def _validate_source_install(
    python: Path,
    *,
    source: Path,
    subject: str,
    side: Literal["reference", "candidate"],
    workspace_root: Path,
    environment: dict[str, str],
) -> None:
    """Prove installed metadata and imports both resolve to the declared checkout."""

    names = _source_import_names(source, subject)
    try:
        completed = subprocess.run(
            [
                str(python),
                "-c",
                _SOURCE_ORIGIN_PROBE,
                subject,
                str(source),
                json.dumps(names),
            ],
            cwd=workspace_root,
            env=_origin_probe_environment(environment),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise WorkspaceError(f"{side} editable-source verification could not start") from exc
    if completed.returncode != 0 or completed.stdout.strip() != "verified":
        raise WorkspaceError(
            f"{side} worker did not import the subject distribution from its declared checkout; "
            "the editable install may be missing or shadowed"
        )


def _validate_local_source_installs(setup: WorkspaceSetup, config: ParityConfig) -> None:
    """Validate editable identity under every effective worker environment."""

    workspace = setup.workspace
    if not workspace.has_local_sources:
        return
    subject = workspace.subject_name
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for lane in setup.lanes:
        for case in config.cases:
            checks: list[tuple[Literal["reference", "candidate"], Path, Path, CallableSpec]] = []
            if workspace.reference_path is not None:
                checks.append(
                    (
                        "reference",
                        lane.reference_python,
                        workspace.reference_path,
                        case.reference,
                    )
                )
            if workspace.candidate_path is not None:
                checks.append(
                    (
                        "candidate",
                        lane.candidate_python,
                        workspace.candidate_path,
                        case.candidate,
                    )
                )
            for side, python, source, endpoint in checks:
                environment_items = tuple(sorted(endpoint.environment.items()))
                marker = (str(python), side, environment_items)
                if marker in seen:
                    continue
                seen.add(marker)
                _validate_source_install(
                    python,
                    source=source,
                    subject=subject,
                    side=side,
                    workspace_root=workspace.root,
                    environment=dict(environment_items),
                )


def run_workspace(
    path: str | Path = "parity.workspace.toml",
    *,
    refresh_locks: bool = False,
    progress: Callable[[Literal["setup", "lane", "complete"], str | None], None] | None = None,
) -> WorkspaceRunResult:
    """Prepare the workspace and run its migration gate in every lane."""

    _invalidate_declared_workspace_reports(path)
    workspace = load_workspace(path)
    manifest = load_migration_manifest(workspace.manifest)
    config = _bind_subject_distribution(
        workspace,
        load_config(workspace.config),
    )
    _validate_source_import_isolation(workspace, config)
    source_provenance = _capture_source_provenance(workspace)
    if progress is not None:
        progress("setup", None)
    try:
        setup = _setup_resolved_workspace(
            workspace,
            refresh_locks=refresh_locks,
        )
        _validate_local_source_installs(setup, config)
        _assert_sources_unchanged(workspace, source_provenance)
        report_dir = _report_directory(setup.workspace)
        lane_results: list[LaneMigrationResult] = []
        for lane in setup.lanes:
            _assert_sources_unchanged(workspace, source_provenance)
            _invalidate_lane_report(report_dir, lane.name)
            if progress is not None:
                progress("lane", lane.name)
            effective = config.model_copy(deep=True)
            for case in effective.cases:
                case.reference.python = lane.reference_python
                case.candidate.python = lane.candidate_python
            result = run_migration(manifest, effective)
            _assert_sources_unchanged(workspace, source_provenance)
            _assert_lane_source_provenance(result, source_provenance)
            report = write_migration_json(result, report_dir / f"{lane.name}.json")
            lane_results.append(LaneMigrationResult(name=lane.name, result=result, report=report))
            if progress is not None:
                progress("complete", lane.name)
        _assert_sources_unchanged(workspace, source_provenance)
        source_report = (
            _write_source_provenance(source_provenance, report_dir / _SOURCE_REPORT)
            if source_provenance is not None
            else None
        )
        _assert_sources_unchanged(workspace, source_provenance)
    except BaseException:
        if source_provenance is not None:
            existing = _existing_report_directory(workspace)
            if existing is not None:
                _invalidate_active_reports(existing, [lane.name for lane in workspace.lanes])
        raise
    return WorkspaceRunResult(
        lanes=tuple(lane_results),
        source_provenance=source_report,
    )


__all__ = [
    "LaneEnvironment",
    "LaneMigrationResult",
    "MigrationWorkspace",
    "ResolvedWorkspace",
    "ResolvedWorkspaceLane",
    "SourceRevision",
    "WorkspaceError",
    "WorkspaceLane",
    "WorkspaceRunResult",
    "WorkspaceSetup",
    "WorkspaceSourceProvenance",
    "advance_workspace",
    "load_workspace",
    "parse_lane_options",
    "rebase_workspace_path",
    "render_tox_config",
    "render_workspace",
    "run_workspace",
    "setup_workspace",
    "write_workspace",
]
