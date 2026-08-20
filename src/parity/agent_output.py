"""Stable, data-safe command contracts for coding-agent integrations.

The CLI owns presentation and exit codes.  These models own only the JSON
shape, allowing commands to produce one predictable document without exposing
raw comparison values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, JsonValue, field_validator, model_validator

from parity.models import StrictModel


def _validate_agent_path(path: str) -> str:
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError("agent output paths must be non-empty and single-line")
    return path


AgentPath = Annotated[
    str,
    Field(min_length=1, max_length=4_096),
    AfterValidator(_validate_agent_path),
]
AgentText = Annotated[str, Field(min_length=1, max_length=8_192)]
MachineCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")]


class CommandName(StrEnum):
    """Commands with a stable machine-readable stdout contract."""

    MIGRATION_INIT = "migration.init"
    MIGRATION_VALIDATE = "migration.validate"
    MIGRATION_RUN = "migration.run"
    REPLAY = "replay"


class CommandStatus(StrEnum):
    """Bounded top-level outcomes shared by agent-facing commands."""

    READY_FOR_REVIEW = "ready_for_review"
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class CommandCwd(StrEnum):
    """Named directory from which a suggested command must be run."""

    INVOCATION = "invocation"
    CONFIG = "config"
    WORKSPACE = "workspace"
    ARTIFACT = "artifact"


class CreatedFileKind(StrEnum):
    WORKSPACE = "workspace"
    CONFIG = "config"
    MANIFEST = "manifest"
    ADAPTER = "adapter"
    FIXTURE = "fixture"
    CHECKLIST = "checklist"


class ReportKind(StrEnum):
    MIGRATION = "migration"
    SUITE = "suite"
    SOURCE_PROVENANCE = "source_provenance"
    REPLAY = "replay"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    DEFERRED = "deferred"


class IssueSeverity(StrEnum):
    REVIEW = "review"
    WARNING = "warning"
    ERROR = "error"


class NextCommand(StrictModel):
    """One directly executable argv and its semantic working directory."""

    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: CommandCwd = CommandCwd.INVOCATION

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: list[str]) -> list[str]:
        if any(not argument or "\x00" in argument for argument in argv):
            raise ValueError("command arguments must be non-empty and cannot contain NUL")
        return argv

    @classmethod
    def from_argv(
        cls,
        *argv: str | Path,
        cwd: CommandCwd = CommandCwd.INVOCATION,
    ) -> Self:
        """Build a command while preserving argument boundaries."""

        return cls(argv=[str(argument) for argument in argv], cwd=cwd)


class CreatedFile(StrictModel):
    kind: CreatedFileKind
    path: AgentPath

    @classmethod
    def from_path(cls, kind: CreatedFileKind, path: str | Path) -> Self:
        return cls(kind=kind, path=_path_text(path))


class ReportReference(StrictModel):
    kind: ReportKind
    path: AgentPath
    lane: str | None = Field(default=None, min_length=1, max_length=128)

    @classmethod
    def from_path(
        cls,
        kind: ReportKind,
        path: str | Path,
        *,
        lane: str | None = None,
    ) -> Self:
        return cls(kind=kind, path=_path_text(path), lane=lane)


class ArtifactReference(StrictModel):
    path: AgentPath
    case: str | None = Field(default=None, min_length=1, max_length=256)
    finding_signature: str | None = Field(default=None, min_length=1, max_length=256)
    replay_command: NextCommand | None = None

    @classmethod
    def replayable(
        cls,
        path: str | Path,
        *,
        case: str | None = None,
        finding_signature: str | None = None,
        cwd: CommandCwd = CommandCwd.INVOCATION,
    ) -> Self:
        rendered = _path_text(path)
        return cls(
            path=rendered,
            case=case,
            finding_signature=finding_signature,
            replay_command=NextCommand.from_argv("parity", "replay", rendered, cwd=cwd),
        )


class CommandCheck(StrictModel):
    code: MachineCode
    status: CheckStatus
    message: AgentText
    case: str | None = Field(default=None, min_length=1, max_length=256)
    side: Literal["reference", "candidate"] | None = None
    path: AgentPath | None = None

    @classmethod
    def passed(cls, code: str, message: str, **context: object) -> Self:
        return cls(code=code, status=CheckStatus.PASSED, message=message, **context)

    @classmethod
    def failed(cls, code: str, message: str, **context: object) -> Self:
        return cls(code=code, status=CheckStatus.FAILED, message=message, **context)

    @classmethod
    def deferred(cls, code: str, message: str, **context: object) -> Self:
        return cls(code=code, status=CheckStatus.DEFERRED, message=message, **context)


class CommandIssue(StrictModel):
    code: MachineCode
    severity: IssueSeverity
    message: AgentText
    path: AgentPath | None = None
    case: str | None = Field(default=None, min_length=1, max_length=256)
    side: Literal["reference", "candidate"] | None = None

    @classmethod
    def error(cls, code: str, message: str, **context: object) -> Self:
        return cls(code=code, severity=IssueSeverity.ERROR, message=message, **context)

    @classmethod
    def warning(cls, code: str, message: str, **context: object) -> Self:
        return cls(code=code, severity=IssueSeverity.WARNING, message=message, **context)

    @classmethod
    def review(cls, code: str, message: str, **context: object) -> Self:
        return cls(code=code, severity=IssueSeverity.REVIEW, message=message, **context)


class AgentCommandOutput(StrictModel):
    """One complete JSON document emitted by an agent-facing command."""

    schema_version: Literal[1] = 1
    command: CommandName
    status: CommandStatus
    created_files: list[CreatedFile] = Field(default_factory=list)
    reports: list[ReportReference] = Field(default_factory=list)
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    checks: list[CommandCheck] = Field(default_factory=list)
    issues: list[CommandIssue] = Field(default_factory=list)
    next_commands: list[NextCommand] = Field(default_factory=list)
    result: JsonValue = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version(cls, version: object) -> object:
        if type(version) is not int or version != 1:
            raise ValueError("schema_version must be the integer 1")
        return version

    @model_validator(mode="after")
    def reject_duplicate_references(self) -> AgentCommandOutput:
        _require_unique(
            ((item.kind.value, item.path) for item in self.created_files),
            "created file references",
        )
        _require_unique(
            ((item.kind.value, item.path, item.lane) for item in self.reports),
            "report references",
        )
        _require_unique((item.path for item in self.artifacts), "artifact references")
        _require_unique(
            ((tuple(item.argv), item.cwd.value) for item in self.next_commands),
            "next commands",
        )
        return self

    @classmethod
    def for_error(
        cls,
        command: CommandName,
        *,
        code: str,
        message: str,
        next_commands: Sequence[NextCommand] = (),
    ) -> Self:
        """Construct a bounded operational-error document."""

        return cls(
            command=command,
            status=CommandStatus.ERROR,
            issues=[CommandIssue.error(code, message)],
            next_commands=list(next_commands),
        )

    def rendered(self, *, pretty: bool = False) -> str:
        """Return deterministic JSON suitable for stdout or stderr."""

        return self.model_dump_json(indent=2 if pretty else None) + "\n"


class ChecklistItemId(StrEnum):
    ADAPTER_SEMANTICS = "adapter_semantics"
    FIXTURE_DOMAIN = "fixture_domain"
    MIGRATION_SURFACE = "migration_surface"
    COMPARISON_POLICY = "comparison_policy"


class ChecklistStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


_CHECKLIST_IDS = tuple(ChecklistItemId)
_CHECKLIST_INSTRUCTIONS = {
    ChecklistItemId.ADAPTER_SEMANTICS: (
        "Implement and review the shared behavioural adapter for both package versions."
    ),
    ChecklistItemId.FIXTURE_DOMAIN: (
        "Replace the sample fixture with representative inputs and important edge cases."
    ),
    ChecklistItemId.MIGRATION_SURFACE: (
        "Review the migration ledger so every in-scope behaviour is covered or excluded."
    ),
    ChecklistItemId.COMPARISON_POLICY: (
        "Review ordering, dtype, null, tolerance and exception comparison semantics."
    ),
}


class ContractChecklistItem(StrictModel):
    id: ChecklistItemId
    status: ChecklistStatus = ChecklistStatus.UNRESOLVED
    instruction: AgentText
    files: list[AgentPath] = Field(min_length=1, max_length=8)

    @field_validator("files")
    @classmethod
    def unique_files(cls, files: list[str]) -> list[str]:
        _require_unique(files, "checklist item files")
        return files


class ContractChecklist(StrictModel):
    """Four explicit review decisions required by a generated migration contract."""

    schema_version: Literal[1] = 1
    status: ChecklistStatus = ChecklistStatus.UNRESOLVED
    items: list[ContractChecklistItem] = Field(min_length=4, max_length=4)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version(cls, version: object) -> object:
        if type(version) is not int or version != 1:
            raise ValueError("schema_version must be the integer 1")
        return version

    @model_validator(mode="after")
    def validate_complete_consistent_contract(self) -> ContractChecklist:
        identifiers = [item.id for item in self.items]
        if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(_CHECKLIST_IDS):
            raise ValueError("checklist must contain each fixed item exactly once")
        expected = (
            ChecklistStatus.RESOLVED
            if all(item.status is ChecklistStatus.RESOLVED for item in self.items)
            else ChecklistStatus.UNRESOLVED
        )
        if self.status is not expected:
            raise ValueError("checklist status must match its item statuses")
        return self

    @property
    def unresolved_ids(self) -> tuple[ChecklistItemId, ...]:
        return tuple(item.id for item in self.items if item.status is ChecklistStatus.UNRESOLVED)

    @classmethod
    def for_scaffold(
        cls,
        *,
        adapter: str | Path,
        fixture: str | Path,
        manifest: str | Path,
        config: str | Path,
    ) -> Self:
        """Create the deterministic checklist emitted by ``migration init --scaffold``."""

        files = {
            ChecklistItemId.ADAPTER_SEMANTICS: [_path_text(adapter)],
            ChecklistItemId.FIXTURE_DOMAIN: [_path_text(fixture)],
            ChecklistItemId.MIGRATION_SURFACE: [_path_text(manifest)],
            ChecklistItemId.COMPARISON_POLICY: [_path_text(config)],
        }
        return cls(
            items=[
                ContractChecklistItem(
                    id=identifier,
                    instruction=_CHECKLIST_INSTRUCTIONS[identifier],
                    files=files[identifier],
                )
                for identifier in _CHECKLIST_IDS
            ]
        )

    @classmethod
    def unresolved(
        cls,
        *,
        adapter: str | Path = "migration_adapters.py",
        fixture: str | Path = "fixtures/input.json",
        manifest: str | Path = "migration.toml",
        config: str | Path = "parity.toml",
    ) -> Self:
        """Create a default unresolved scaffold checklist."""

        return cls.for_scaffold(
            adapter=adapter,
            fixture=fixture,
            manifest=manifest,
            config=config,
        )

    def resolving(self, *identifiers: ChecklistItemId | str) -> Self:
        """Return a new checklist with the selected decisions marked resolved."""

        selected = {ChecklistItemId(identifier) for identifier in identifiers}
        items = [
            item.model_copy(
                update={
                    "status": (ChecklistStatus.RESOLVED if item.id in selected else item.status)
                }
            )
            for item in self.items
        ]
        status = (
            ChecklistStatus.RESOLVED
            if all(item.status is ChecklistStatus.RESOLVED for item in items)
            else ChecklistStatus.UNRESOLVED
        )
        return type(self)(status=status, items=items)


def _path_text(path: str | Path) -> str:
    rendered = Path(path).as_posix()
    return _validate_agent_path(rendered)


def _require_unique(values: Iterable[object], label: str) -> None:
    observed = list(values)
    if len(observed) != len(set(observed)):
        raise ValueError(f"{label} must be unique")


__all__ = [
    "AgentCommandOutput",
    "ArtifactReference",
    "CheckStatus",
    "ChecklistItemId",
    "ChecklistStatus",
    "CommandCheck",
    "CommandCwd",
    "CommandIssue",
    "CommandName",
    "CommandStatus",
    "ContractChecklist",
    "ContractChecklistItem",
    "CreatedFile",
    "CreatedFileKind",
    "IssueSeverity",
    "NextCommand",
    "ReportKind",
    "ReportReference",
]
