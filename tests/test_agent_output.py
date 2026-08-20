from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from parity.agent_output import (
    AgentCommandOutput,
    ArtifactReference,
    ChecklistItemId,
    ChecklistStatus,
    CommandCheck,
    CommandCwd,
    CommandIssue,
    CommandName,
    CommandStatus,
    ContractChecklist,
    CreatedFile,
    CreatedFileKind,
    NextCommand,
    ReportKind,
    ReportReference,
)


def test_agent_command_output_round_trips_as_one_stable_json_document() -> None:
    output = AgentCommandOutput(
        command=CommandName.MIGRATION_RUN,
        status=CommandStatus.FAILED,
        created_files=[
            CreatedFile.from_path(CreatedFileKind.WORKSPACE, Path("migrations/workspace.toml"))
        ],
        reports=[
            ReportReference.from_path(
                ReportKind.MIGRATION,
                Path("migrations/reports/default.json"),
                lane="default",
            )
        ],
        artifacts=[
            ArtifactReference.replayable(
                Path("migrations/.parity/orders/finding"),
                case="orders",
                finding_signature="ms3:" + "a" * 64,
                cwd=CommandCwd.CONFIG,
            )
        ],
        checks=[CommandCheck.passed("workspace.document", "workspace is valid")],
        issues=[CommandIssue.review("adapter.semantics", "adapter needs review")],
        next_commands=[
            NextCommand.from_argv(
                "parity",
                "replay",
                "migrations/.parity/orders/finding",
                cwd=CommandCwd.CONFIG,
            )
        ],
    )

    rendered = output.rendered()
    payload = json.loads(rendered)
    restored = AgentCommandOutput.model_validate_json(rendered)

    assert rendered.endswith("\n")
    assert payload["schema_version"] == 1
    assert payload["command"] == "migration.run"
    assert payload["artifacts"][0]["replay_command"] == {
        "argv": ["parity", "replay", "migrations/.parity/orders/finding"],
        "cwd": "config",
    }
    assert restored == output


def test_agent_output_defaults_and_error_constructor_are_bounded() -> None:
    output = AgentCommandOutput.for_error(
        CommandName.REPLAY,
        code="artifact.missing",
        message="artifact could not be read",
    )

    assert output.status is CommandStatus.ERROR
    assert output.created_files == []
    assert output.reports == []
    assert output.artifacts == []
    assert output.checks == []
    assert output.next_commands == []
    assert output.issues == [CommandIssue.error("artifact.missing", "artifact could not be read")]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentCommandOutput.model_validate(
            {
                "command": "replay",
                "status": "error",
                "unexpected": True,
            }
        )
    for invalid_version in (True, 1.0, 2):
        with pytest.raises(ValidationError, match="schema_version must be the integer 1"):
            AgentCommandOutput.model_validate(
                {
                    "schema_version": invalid_version,
                    "command": "replay",
                    "status": "error",
                }
            )


def test_commands_and_references_reject_ambiguous_or_duplicate_values() -> None:
    with pytest.raises(ValidationError, match="command arguments must be non-empty"):
        NextCommand(argv=["parity", ""])
    with pytest.raises(ValidationError, match="created file references must be unique"):
        AgentCommandOutput(
            command=CommandName.MIGRATION_INIT,
            status=CommandStatus.READY_FOR_REVIEW,
            created_files=[
                CreatedFile(kind=CreatedFileKind.CONFIG, path="migrations/parity.toml"),
                CreatedFile(kind=CreatedFileKind.CONFIG, path="migrations/parity.toml"),
            ],
        )
    with pytest.raises(ValidationError, match="String should match pattern"):
        CommandIssue.error("INVALID CODE", "bad machine code")


def test_scaffold_checklist_has_exact_fixed_unresolved_contract() -> None:
    checklist = ContractChecklist.for_scaffold(
        adapter="migrations/migration_adapters.py",
        fixture="migrations/fixtures/input.json",
        manifest="migrations/migration.toml",
        config="migrations/parity.toml",
    )

    assert checklist.status is ChecklistStatus.UNRESOLVED
    assert checklist.unresolved_ids == tuple(ChecklistItemId)
    assert [item.id for item in checklist.items] == list(ChecklistItemId)
    assert [item.files for item in checklist.items] == [
        ["migrations/migration_adapters.py"],
        ["migrations/fixtures/input.json"],
        ["migrations/migration.toml"],
        ["migrations/parity.toml"],
    ]
    assert ContractChecklist.model_validate_json(checklist.model_dump_json()) == checklist
    assert ContractChecklist.unresolved() == ContractChecklist.for_scaffold(
        adapter="migration_adapters.py",
        fixture="fixtures/input.json",
        manifest="migration.toml",
        config="parity.toml",
    )


def test_checklist_resolution_is_immutable_and_derives_overall_status() -> None:
    original = ContractChecklist.for_scaffold(
        adapter="adapter.py",
        fixture="fixture.json",
        manifest="migration.toml",
        config="parity.toml",
    )

    partial = original.resolving(ChecklistItemId.ADAPTER_SEMANTICS, "fixture_domain")
    resolved = partial.resolving(*ChecklistItemId)

    assert original.status is ChecklistStatus.UNRESOLVED
    assert len(original.unresolved_ids) == 4
    assert partial.status is ChecklistStatus.UNRESOLVED
    assert partial.unresolved_ids == (
        ChecklistItemId.MIGRATION_SURFACE,
        ChecklistItemId.COMPARISON_POLICY,
    )
    assert resolved.status is ChecklistStatus.RESOLVED
    assert resolved.unresolved_ids == ()


def test_checklist_rejects_missing_duplicate_and_inconsistent_state() -> None:
    checklist = ContractChecklist.for_scaffold(
        adapter="adapter.py",
        fixture="fixture.json",
        manifest="migration.toml",
        config="parity.toml",
    )

    with pytest.raises(ValidationError, match="at least 4 items"):
        ContractChecklist(items=checklist.items[:-1])
    with pytest.raises(ValidationError, match="each fixed item exactly once"):
        ContractChecklist(items=[checklist.items[0], *checklist.items[:3]])
    with pytest.raises(ValidationError, match="status must match"):
        ContractChecklist(status=ChecklistStatus.RESOLVED, items=checklist.items)
