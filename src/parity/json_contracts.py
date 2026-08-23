"""Versioned JSON Schemas for authored inputs and data-safe public outputs."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, JsonValue, model_validator

from parity.agent_output import AgentCommandOutput, ContractChecklist
from parity.config import _CaseDefaults
from parity.distilled import DistilledContractManifest
from parity.migration import (
    MigrationCaseStatus,
    MigrationManifest,
    MigrationUnitStatus,
)
from parity.migration_workspace import MigrationWorkspace
from parity.models import (
    CaseConfig,
    CaseProvenance,
    CompatibilityBudget,
    CompatibilityResult,
    Diagnosis,
    InvocationDocument,
    MismatchKind,
    PerformanceResult,
    RunMetrics,
    Status,
    StrictModel,
    SuiteProvenance,
)


class ConfigContract(StrictModel):
    """Structurally typed authoring contract for ``parity.toml``."""

    version: Literal[2] = 2
    artifact_dir: Path = Path(".parity")
    cases: list[CaseConfig] | None = Field(default=None, min_length=1)
    cases_file: Path | None = None
    case_defaults: _CaseDefaults | None = None
    fail_fast: bool = False
    jobs: int = Field(default=1, ge=1, le=256)
    native_threads: int | None = Field(default=None, ge=1, le=256)
    compatibility_budget: Path | None = None

    @model_validator(mode="after")
    def require_one_case_source(self) -> ConfigContract:
        if (self.cases is None) == (self.cases_file is None):
            raise ValueError("exactly one of cases or cases_file is required")
        return self


class ReturnOutcomeContract(StrictModel):
    """A public exception-comparison side that returned normally."""

    outcome: Literal["return"]


class RaiseOutcomeContract(StrictModel):
    """A public exception-comparison side that raised."""

    outcome: Literal["raise"]
    exception_type: str = Field(min_length=1)
    location_shapes: list[str] | None = None
    errno: int | None = None
    api_tokens: list[str] | None = None
    error_codes: list[str] | None = None
    member_types: list[str] | None = None


SemanticOutcomeContract = Annotated[
    ReturnOutcomeContract | RaiseOutcomeContract,
    Field(discriminator="outcome"),
]


class MismatchSummaryContract(StrictModel):
    """Data-safe structural evidence for one mismatch in a finding."""

    kind: MismatchKind
    summary: str = Field(min_length=1)
    path: str = Field(min_length=1)
    reference: SemanticOutcomeContract | None = None
    candidate: SemanticOutcomeContract | None = None


MismatchCount = Annotated[int, Field(ge=1)]


class MismatchCountsContract(StrictModel):
    """Counts for the finite public mismatch taxonomy."""

    column: MismatchCount | None = None
    dtype: MismatchCount | None = None
    exception: MismatchCount | None = None
    mutation: MismatchCount | None = None
    performance: MismatchCount | None = None
    row: MismatchCount | None = None
    schema_: MismatchCount | None = Field(
        default=None,
        validation_alias="schema",
        serialization_alias="schema",
    )
    shape: MismatchCount | None = None
    value: MismatchCount | None = None


class FindingContract(StrictModel):
    """One data-safe finding or operational error embedded in a suite report."""

    source: str
    status: Status
    finding_signature: str | None = Field(default=None, pattern=r"^ms3:[0-9a-f]{64}$")
    approved: bool
    mismatch_counts: MismatchCountsContract
    mismatches: list[MismatchSummaryContract]
    artifact: str | None = None
    reference_metrics: RunMetrics | None = None
    candidate_metrics: RunMetrics | None = None

    @model_validator(mode="after")
    def validate_approval(self) -> FindingContract:
        if self.approved and (self.status is not Status.FAILED or self.finding_signature is None):
            raise ValueError("only a signed semantic finding can be approved")
        return self


class SuiteCaseReportContract(StrictModel):
    """Data-safe result projection for one configured case."""

    name: str = Field(min_length=1)
    status: Status
    examples_run: int = Field(ge=0)
    deterministic_examples: int = Field(ge=0)
    generated_examples: int = Field(ge=0)
    findings_discovered: int = Field(ge=0)
    finding_limit_reached: bool
    failures: list[FindingContract]
    diagnoses: list[Diagnosis]
    performance: PerformanceResult | None
    provenance: CaseProvenance | None
    compatibility: CompatibilityResult | None
    elapsed_seconds: float = Field(ge=0)


class SuiteReportContract(StrictModel):
    """Data-eliding projection returned by :func:`parity.reporting.report_payload`."""

    schema_version: Literal[4]
    status: Status
    cases: list[SuiteCaseReportContract]
    elapsed_seconds: float = Field(ge=0)
    parity_version: str
    provenance: SuiteProvenance | None = None


class MigrationSummaryContract(StrictModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    error: int = Field(ge=0)
    excluded: int = Field(ge=0)
    uncovered: int = Field(ge=0)


class MigrationCaseEvidenceContract(StrictModel):
    """Data-safe execution evidence for one case mapped to a migration unit."""

    name: str = Field(min_length=1)
    status: MigrationCaseStatus
    examples_run: int | None = Field(default=None, ge=0)


class MigrationUnitReportContract(StrictModel):
    """Data-safe report projection for one declared migration unit."""

    id: str = Field(min_length=1)
    status: MigrationUnitStatus
    cases: list[MigrationCaseEvidenceContract]
    excluded_reason: str | None = None


class MigrationReportContract(StrictModel):
    """Data-eliding projection returned by ``migration_report_payload``."""

    schema_version: Literal[1]
    status: Status
    summary: MigrationSummaryContract
    units: list[MigrationUnitReportContract]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parity: SuiteReportContract


class ReplayPathBaseContract(StrictModel):
    kind: Literal["artifact_ancestor"]
    levels: int = Field(ge=1, le=64)


class ReplayInputContract(StrictModel):
    name: str = Field(min_length=1)
    file: str = Field(min_length=1)


class ReplayContract(StrictModel):
    """Path-free replay document stored inside a finding artifact."""

    version: Literal[3]
    path_base: ReplayPathBaseContract | None = None
    case: dict[str, JsonValue]
    environment: str
    invocation: InvocationDocument
    replay_blockers: dict[str, str] | None = None
    expected_runtime: JsonValue = None
    config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    command: list[str] | None = None


class ArtifactFileContract(StrictModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class ArtifactManifestContract(StrictModel):
    version: Literal[3]
    campaign_id: str = Field(min_length=1)
    case: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    source: str | None = None
    seed: int | None = None
    contains_input_data: Literal[True]
    files: dict[str, ArtifactFileContract]


_CONTRACTS: dict[str, tuple[type[Any], int]] = {
    "agent-result": (AgentCommandOutput, 1),
    "artifact-manifest": (ArtifactManifestContract, 3),
    "checklist": (ContractChecklist, 1),
    "compatibility-budget": (CompatibilityBudget, 1),
    "config": (ConfigContract, 2),
    "distilled-contract": (DistilledContractManifest, 3),
    "finding": (FindingContract, 1),
    "migration-manifest": (MigrationManifest, 1),
    "migration-report": (MigrationReportContract, 1),
    "replay": (ReplayContract, 3),
    "suite-report": (SuiteReportContract, 4),
    "workspace": (MigrationWorkspace, 3),
}


def contract_names() -> tuple[str, ...]:
    """Return every published contract name in deterministic order."""

    return tuple(sorted(_CONTRACTS))


def contract_schema(name: str) -> dict[str, Any]:
    """Return an isolated copy of one frozen Draft 2020-12 schema."""

    _contract(name)
    resource = files("parity.schemas").joinpath(f"{name}.json")
    try:
        loaded = json.loads(resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:  # pragma: no cover - package damage
        raise RuntimeError(f"packaged schema resource is invalid or missing: {name}") from exc
    if not isinstance(loaded, dict):  # pragma: no cover - package damage
        raise RuntimeError(f"packaged schema resource is not an object: {name}")
    return cast(dict[str, Any], loaded)


def _generated_contract_schema(name: str) -> dict[str, Any]:
    """Generate a schema only for the deliberate resource-freezing script."""

    model, version = _contract(name)
    schema = cast(dict[str, Any], model.model_json_schema(mode="validation"))
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://parity-check.dev/schemas/{name}/v{version}.json"
    schema["x-parity-contract"] = name
    schema["x-parity-contract-version"] = version
    if name == "config":
        schema["allOf"] = [_exactly_one("cases", "cases_file")]
    elif name == "workspace":
        schema["allOf"] = [
            _exactly_one("reference_package", "reference_path"),
            _exactly_one("candidate_package", "candidate_path"),
            {
                "oneOf": [
                    {"required": ["python"]},
                    {
                        "required": ["reference_python", "candidate_python"],
                        "not": {"required": ["python"]},
                    },
                ]
            },
        ]
    return schema


def _contract(name: str) -> tuple[type[Any], int]:
    """Resolve one public contract name."""

    try:
        return _CONTRACTS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown schema {name!r}; choose one of: {', '.join(contract_names())}"
        ) from exc


def _exactly_one(left: str, right: str) -> dict[str, Any]:
    return {
        "oneOf": [
            {"required": [left], "not": {"required": [right]}},
            {"required": [right], "not": {"required": [left]}},
        ]
    }


__all__ = [
    "ArtifactManifestContract",
    "ConfigContract",
    "FindingContract",
    "MigrationCaseEvidenceContract",
    "MigrationReportContract",
    "MigrationUnitReportContract",
    "MismatchCountsContract",
    "MismatchSummaryContract",
    "ReplayContract",
    "SuiteCaseReportContract",
    "SuiteReportContract",
    "contract_names",
    "contract_schema",
]
