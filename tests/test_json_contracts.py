from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from parity.json_contracts import FindingContract, contract_names, contract_schema


def test_every_public_schema_is_self_describing_and_deterministic() -> None:
    assert contract_names() == (
        "agent-result",
        "artifact-manifest",
        "checklist",
        "config",
        "distilled-contract",
        "finding",
        "migration-manifest",
        "migration-report",
        "replay",
        "suite-report",
        "workspace",
    )

    for name in contract_names():
        first = contract_schema(name)
        second = contract_schema(name)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        assert first["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert first["$id"].startswith(f"https://parity-check.dev/schemas/{name}/v")
        assert first["x-parity-contract"] == name
        assert first["additionalProperties"] is False


def test_breaking_contract_versions_are_exposed_in_their_schemas() -> None:
    workspace = contract_schema("workspace")
    replay = contract_schema("replay")
    artifact = contract_schema("artifact-manifest")

    assert workspace["properties"]["version"]["const"] == 3
    assert replay["properties"]["version"]["const"] == 2
    assert artifact["properties"]["version"]["const"] == 2
    assert replay["$id"].endswith("/v2.json")


def test_report_schemas_describe_cases_findings_and_migration_units() -> None:
    finding = contract_schema("finding")
    suite = contract_schema("suite-report")
    migration = contract_schema("migration-report")
    config = contract_schema("config")

    assert finding["properties"]["mismatch_counts"] == {"$ref": "#/$defs/MismatchCountsContract"}
    assert finding["$defs"]["MismatchCountsContract"]["additionalProperties"] is False
    assert finding["$defs"]["MismatchSummaryContract"]["properties"]["kind"] == {
        "$ref": "#/$defs/MismatchKind"
    }
    assert suite["properties"]["cases"]["items"] == {"$ref": "#/$defs/SuiteCaseReportContract"}
    assert suite["$defs"]["SuiteCaseReportContract"]["properties"]["failures"]["items"] == {
        "$ref": "#/$defs/FindingContract"
    }
    assert migration["properties"]["units"]["items"] == {
        "$ref": "#/$defs/MigrationUnitReportContract"
    }
    assert migration["$defs"]["MigrationUnitReportContract"]["properties"]["cases"]["items"] == {
        "$ref": "#/$defs/MigrationCaseEvidenceContract"
    }
    inline_cases = config["properties"]["cases"]["anyOf"][0]
    assert inline_cases["items"] == {"$ref": "#/$defs/CaseConfig"}
    assert {"name", "reference", "candidate"} <= set(config["$defs"]["CaseConfig"]["required"])


def test_finding_contract_rejects_unknown_nested_fields_and_mismatch_kinds() -> None:
    finding = {
        "source": "generated",
        "status": "failed",
        "finding_signature": "ms3:" + "a" * 64,
        "mismatch_counts": {"value": 1},
        "mismatches": [{"kind": "value", "summary": "values differ", "path": "$"}],
        "artifact": None,
        "reference_metrics": None,
        "candidate_metrics": None,
    }

    assert (
        FindingContract.model_validate(finding).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        == finding
    )

    unknown_evidence = dict(finding)
    unknown_evidence["mismatches"] = [
        {"kind": "value", "summary": "values differ", "path": "$", "observed": "secret"}
    ]
    with pytest.raises(ValidationError, match="observed"):
        FindingContract.model_validate(unknown_evidence)

    unknown_kind = dict(finding)
    unknown_kind["mismatch_counts"] = {"private": 1}
    with pytest.raises(ValidationError, match="private"):
        FindingContract.model_validate(unknown_kind)
