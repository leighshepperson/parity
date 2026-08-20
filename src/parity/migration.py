"""Migration-surface coverage gate built on configured Parity cases.

The migration ledger is deliberately separate from ``parity.toml``.  The
latter remains the executable authority for reference and candidate callables;
the ledger only declares which configured cases cover each migration unit.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from parity.config import load_config
from parity.execution import redact_text
from parity.models import CaseResult, ParityConfig, Status, StrictModel, SuiteResult
from parity.reporting import report_payload

MigrationName = Annotated[
    str,
    Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$"),
]


class MigrationConfigError(ValueError):
    """Raised when a migration manifest cannot be loaded or mapped safely."""


class MigrationUnitStatus(StrEnum):
    """Outcome of one declared migration unit."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    EXCLUDED = "excluded"
    UNCOVERED = "uncovered"


class MigrationCaseStatus(StrEnum):
    """Coverage evidence contributed by one mapped Parity case."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    MISSING = "missing"
    NOT_EXERCISED = "not_exercised"


class MigrationUnit(StrictModel):
    """One public behavior and the configured Parity cases covering it."""

    id: MigrationName
    cases: list[MigrationName] = Field(default_factory=list)
    excluded_reason: str | None = None

    @field_validator("cases")
    @classmethod
    def unique_cases(cls, cases: list[str]) -> list[str]:
        if len(cases) != len(set(cases)):
            raise ValueError("migration unit cases must be unique")
        return cases

    @field_validator("excluded_reason")
    @classmethod
    def normalize_excluded_reason(cls, reason: str | None) -> str | None:
        if reason is None:
            return None
        normalized = reason.strip()
        if not normalized:
            raise ValueError("excluded_reason cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_state(self) -> MigrationUnit:
        if self.cases and self.excluded_reason is not None:
            raise ValueError("a migration unit cannot have both cases and excluded_reason")
        return self


class MigrationManifest(StrictModel):
    """Versioned migration-ledger document."""

    version: Literal[1] = 1
    units: list[MigrationUnit] = Field(min_length=1)

    @field_validator("units")
    @classmethod
    def unique_unit_ids(cls, units: list[MigrationUnit]) -> list[MigrationUnit]:
        identifiers = [unit.id for unit in units]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("migration unit ids must be unique")
        return units


class MigrationCaseEvidence(StrictModel):
    """Bounded execution evidence for one case mapped to a migration unit."""

    name: MigrationName
    status: MigrationCaseStatus
    examples_run: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_evidence(self) -> MigrationCaseEvidence:
        if self.status is MigrationCaseStatus.MISSING and self.examples_run is not None:
            raise ValueError("missing case evidence cannot report examples")
        if self.status is MigrationCaseStatus.NOT_EXERCISED and self.examples_run != 0:
            raise ValueError("not_exercised case evidence requires zero examples")
        if self.status is MigrationCaseStatus.PASSED and (
            self.examples_run is None or self.examples_run < 1
        ):
            raise ValueError("passed case evidence requires at least one example")
        return self


_CASE_ERROR_STATUSES = frozenset(
    {
        MigrationCaseStatus.ERROR,
        MigrationCaseStatus.SKIPPED,
        MigrationCaseStatus.MISSING,
        MigrationCaseStatus.NOT_EXERCISED,
    }
)


class MigrationUnitResult(StrictModel):
    """Derived coverage status for one manifest unit."""

    id: MigrationName
    status: MigrationUnitStatus
    cases: list[MigrationCaseEvidence] = Field(default_factory=list)
    excluded_reason: str | None = None

    @model_validator(mode="after")
    def validate_result_state(self) -> MigrationUnitResult:
        if self.status is MigrationUnitStatus.EXCLUDED:
            if self.excluded_reason is None or not self.excluded_reason.strip():
                raise ValueError("excluded migration results require a reason")
            if self.cases:
                raise ValueError("excluded migration results cannot contain case evidence")
            return self
        if self.status is MigrationUnitStatus.UNCOVERED:
            if self.cases or self.excluded_reason is not None:
                raise ValueError("uncovered migration results cannot contain cases or a reason")
            return self
        if not self.cases:
            raise ValueError("evaluated migration results require case evidence")
        if self.excluded_reason is not None:
            raise ValueError("evaluated migration results cannot contain an exclusion reason")

        statuses = {case.status for case in self.cases}
        if self.status is MigrationUnitStatus.ERROR and not statuses.intersection(
            _CASE_ERROR_STATUSES
        ):
            raise ValueError("error migration results require error case evidence")
        if self.status is MigrationUnitStatus.FAILED and (
            MigrationCaseStatus.FAILED not in statuses
            or statuses.intersection(_CASE_ERROR_STATUSES)
        ):
            raise ValueError("failed migration results require failure without error evidence")
        if self.status is MigrationUnitStatus.PASSED and statuses != {MigrationCaseStatus.PASSED}:
            raise ValueError("passed migration results require every case to pass")
        return self


class MigrationResult(StrictModel):
    """Complete result graph for one migration coverage gate."""

    status: Status
    units: list[MigrationUnitResult]
    suite: SuiteResult
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_overall_status(self) -> MigrationResult:
        if self.status is Status.SKIPPED:
            raise ValueError("a migration result cannot be skipped")
        identifiers = [unit.id for unit in self.units]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("migration result unit ids must be unique")
        expected = _migration_status(self.units, suite_status=self.suite.status)
        if self.status is not expected:
            raise ValueError(
                f"migration status {self.status.value!r} contradicts derived {expected.value!r}"
            )
        return self

    @property
    def passed(self) -> bool:
        """Whether every declared in-scope unit passed."""

        return self.status is Status.PASSED


def load_migration_manifest(path: str | Path = "migration.toml") -> MigrationManifest:
    """Load and validate a versioned migration manifest."""

    manifest_path = Path(path).resolve()
    try:
        raw: dict[str, Any] = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationConfigError(f"migration manifest not found: {manifest_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise MigrationConfigError(f"invalid TOML in {manifest_path}: {exc}") from exc
    try:
        return MigrationManifest.model_validate(raw)
    except ValueError as exc:
        raise MigrationConfigError(f"invalid migration manifest: {exc}") from exc


def migration_manifest_sha256(manifest: MigrationManifest) -> str:
    """Return a deterministic fingerprint of the effective manifest contract."""

    encoded = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_migration_manifest(manifest: MigrationManifest) -> str:
    """Render the small, reviewable migration ledger as TOML."""

    lines = ["# migration.toml — review this declared migration surface", "version = 1", ""]
    for unit in manifest.units:
        lines.extend(["[[units]]", f"id = {json.dumps(unit.id)}"])
        if unit.excluded_reason is not None:
            lines.append(f"excluded_reason = {json.dumps(unit.excluded_reason)}")
        else:
            cases = ", ".join(json.dumps(case) for case in unit.cases)
            lines.append(f"cases = [{cases}]")
        lines.append("")
    return "\n".join(lines)


def write_migration_manifest(
    manifest: MigrationManifest,
    destination: str | Path,
    *,
    force: bool = False,
) -> Path:
    """Atomically publish a manifest without replacing one by default."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(render_migration_manifest(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            if os.path.lexists(path) and not (path.is_file() or path.is_symlink()):
                raise MigrationConfigError("migration manifest destination is not a file")
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise FileExistsError(f"migration manifest already exists: {path}") from None
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _mapped_cases(manifest: MigrationManifest) -> set[str]:
    return {case for unit in manifest.units for case in unit.cases}


def _validate_case_mapping(manifest: MigrationManifest, config: ParityConfig) -> set[str]:
    selected = _mapped_cases(manifest)
    known = {case.name for case in config.cases}
    if unknown := selected - known:
        raise MigrationConfigError(
            f"migration manifest references unknown case(s): {', '.join(sorted(unknown))}"
        )
    return selected


def _case_evidence(name: str, case: CaseResult | None) -> MigrationCaseEvidence:
    if case is None:
        return MigrationCaseEvidence(name=name, status=MigrationCaseStatus.MISSING)
    if case.status is Status.PASSED:
        status = (
            MigrationCaseStatus.PASSED
            if case.examples_run > 0
            else MigrationCaseStatus.NOT_EXERCISED
        )
    else:
        status = MigrationCaseStatus(case.status.value)
    return MigrationCaseEvidence(
        name=name,
        status=status,
        examples_run=case.examples_run,
    )


def _unit_result(
    unit: MigrationUnit,
    cases_by_name: dict[str, CaseResult],
) -> MigrationUnitResult:
    if unit.excluded_reason is not None:
        return MigrationUnitResult(
            id=unit.id,
            status=MigrationUnitStatus.EXCLUDED,
            excluded_reason=unit.excluded_reason,
        )
    if not unit.cases:
        return MigrationUnitResult(id=unit.id, status=MigrationUnitStatus.UNCOVERED)

    evidence = [_case_evidence(name, cases_by_name.get(name)) for name in unit.cases]
    statuses = {case.status for case in evidence}
    if statuses.intersection(_CASE_ERROR_STATUSES):
        status = MigrationUnitStatus.ERROR
    elif MigrationCaseStatus.FAILED in statuses:
        status = MigrationUnitStatus.FAILED
    else:
        status = MigrationUnitStatus.PASSED
    return MigrationUnitResult(id=unit.id, status=status, cases=evidence)


def _migration_status(
    units: list[MigrationUnitResult],
    *,
    suite_status: Status,
) -> Status:
    if suite_status in {Status.ERROR, Status.SKIPPED} or any(
        unit.status is MigrationUnitStatus.ERROR for unit in units
    ):
        return Status.ERROR
    if suite_status is Status.FAILED or any(
        unit.status in {MigrationUnitStatus.FAILED, MigrationUnitStatus.UNCOVERED} for unit in units
    ):
        return Status.FAILED
    # An all-excluded manifest is not evidence that a migration succeeded.
    if not any(unit.status is MigrationUnitStatus.PASSED for unit in units):
        return Status.FAILED
    return Status.PASSED


def run_migration(manifest: MigrationManifest, config: ParityConfig) -> MigrationResult:
    """Run every mapped case once and evaluate declared migration coverage.

    The config is copied before ``fail_fast`` is disabled, so callers retain
    their original object while this completeness gate always attempts the
    entire selected case union.  Passing an empty set is intentional: ``None``
    would tell the engine to run every configured case.
    """

    selected = _validate_case_mapping(manifest, config)
    effective_config = config.model_copy(deep=True)
    effective_config.fail_fast = False

    from parity.engine import run_suite

    suite = run_suite(effective_config, selected_cases=selected)
    returned_names = [case.name for case in suite.cases]
    if len(returned_names) != len(set(returned_names)):
        raise RuntimeError("migration verification returned duplicate case results")
    unexpected = set(returned_names) - selected
    if unexpected:
        raise RuntimeError(
            "migration verification returned unexpected case result(s): "
            + ", ".join(sorted(unexpected))
        )
    cases_by_name = {case.name: case for case in suite.cases}
    units = [_unit_result(unit, cases_by_name) for unit in manifest.units]
    status = _migration_status(units, suite_status=suite.status)
    return MigrationResult(
        status=status,
        units=units,
        suite=suite,
        manifest_sha256=migration_manifest_sha256(manifest),
    )


def check_migration(
    manifest: str | Path = "migration.toml",
    config: str | Path = "parity.toml",
) -> MigrationResult:
    """Load a manifest and Parity config, then run the migration coverage gate."""

    return run_migration(load_migration_manifest(manifest), load_config(config))


def migration_summary(result: MigrationResult) -> dict[str, int]:
    """Derive non-contradictory unit counts from result evidence."""

    counts = Counter(unit.status for unit in result.units)
    return {
        "total": len(result.units),
        "passed": counts[MigrationUnitStatus.PASSED],
        "failed": counts[MigrationUnitStatus.FAILED],
        "error": counts[MigrationUnitStatus.ERROR],
        "excluded": counts[MigrationUnitStatus.EXCLUDED],
        "uncovered": counts[MigrationUnitStatus.UNCOVERED],
    }


def migration_report_payload(result: MigrationResult) -> dict[str, Any]:
    """Return the stable, data-eliding migration JSON report contract."""

    payload = {
        "schema_version": 1,
        "status": result.status.value,
        "summary": migration_summary(result),
        "units": [
            {
                "id": redact_text(unit.id),
                "status": unit.status.value,
                "cases": [
                    {
                        "name": redact_text(case.name),
                        "status": case.status.value,
                        "examples_run": case.examples_run,
                    }
                    for case in unit.cases
                ],
                "excluded_reason": (
                    redact_text(unit.excluded_reason) if unit.excluded_reason is not None else None
                ),
            }
            for unit in result.units
        ],
        "manifest_sha256": result.manifest_sha256,
        # Never model_dump the raw suite here: mismatch evidence can contain
        # compared values.  The existing report projection deliberately elides
        # those values and retains the selected effective-config fingerprint.
        "parity": report_payload(result.suite),
    }
    from parity.json_contracts import MigrationReportContract

    return MigrationReportContract.model_validate(payload).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )


def render_migration_json(result: MigrationResult, *, pretty: bool = True) -> str:
    """Render a machine-readable migration report."""

    return json.dumps(
        migration_report_payload(result),
        indent=2 if pretty else None,
        sort_keys=pretty,
        allow_nan=False,
        separators=None if pretty else (",", ":"),
    ) + ("\n" if pretty else "")


def write_migration_json(result: MigrationResult, destination: str | Path) -> Path:
    """Atomically write a data-eliding migration report."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(render_migration_json(result))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "MigrationCaseEvidence",
    "MigrationCaseStatus",
    "MigrationConfigError",
    "MigrationManifest",
    "MigrationResult",
    "MigrationUnit",
    "MigrationUnitResult",
    "MigrationUnitStatus",
    "check_migration",
    "load_migration_manifest",
    "migration_manifest_sha256",
    "migration_report_payload",
    "migration_summary",
    "render_migration_json",
    "render_migration_manifest",
    "run_migration",
    "write_migration_json",
    "write_migration_manifest",
]
