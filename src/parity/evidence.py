"""Batch verification for counterexamples referenced by data-safe reports."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from parity._version import __version__
from parity.execution import redact_text
from parity.models import ExampleResult, Status, StrictModel


class EvidenceError(ValueError):
    """Raised when a report cannot safely identify replayable evidence."""


class EvidenceArtifactStatus(StrEnum):
    """Outcome of verifying one report-referenced artifact."""

    VERIFIED = "verified"
    STALE = "stale"
    ERROR = "error"


class EvidenceArtifactResult(StrictModel):
    """Data-safe verification result for one counterexample artifact."""

    case: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    artifact: str = Field(min_length=1)
    status: EvidenceArtifactStatus
    expected_signature: str = Field(pattern=r"^ms1:[0-9a-f]{64}$")
    actual_signature: str | None = Field(default=None, pattern=r"^ms1:[0-9a-f]{64}$")

    @field_validator("artifact")
    @classmethod
    def safe_artifact_name(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact report paths must be relative and contained")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("artifact report paths cannot contain control characters")
        return value

    @model_validator(mode="after")
    def validate_signature_outcome(self) -> EvidenceArtifactResult:
        if self.status is EvidenceArtifactStatus.VERIFIED:
            if self.actual_signature != self.expected_signature:
                raise ValueError("verified evidence must reproduce its expected signature")
        elif self.status is EvidenceArtifactStatus.STALE:
            if self.actual_signature == self.expected_signature:
                raise ValueError("stale evidence cannot reproduce only its expected signature")
        elif self.actual_signature is not None:
            raise ValueError("errored evidence cannot claim an actual signature")
        return self


class EvidenceResult(StrictModel):
    """Complete batch-verification result for one source report."""

    status: Status
    parity_version: str = __version__
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: list[EvidenceArtifactResult] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_status(self) -> EvidenceResult:
        if self.status is Status.SKIPPED:
            raise ValueError("evidence verification cannot be skipped")
        expected = _result_status(self.artifacts)
        if self.status is not expected:
            raise ValueError(
                f"evidence status {self.status.value!r} contradicts {expected.value!r}"
            )
        return self


def _suite_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema_version = payload.get("schema_version")
    if type(schema_version) is int and schema_version == 3:
        suite = payload
    elif (
        type(schema_version) is int
        and schema_version == 1
        and isinstance(payload.get("parity"), dict)
    ):
        suite = payload["parity"]
    else:
        raise EvidenceError("report must be Parity suite schema 3 or migration schema 1")
    if type(suite.get("schema_version")) is not int or suite.get("schema_version") != 3:
        raise EvidenceError("report contains an unsupported Parity result payload")
    if not isinstance(suite.get("cases"), list):
        raise EvidenceError("report contains an unsupported Parity result payload")
    return suite


def _report_entries(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    artifact_bindings: dict[str, tuple[str, str]] = {}
    for raw_case in _suite_payload(payload)["cases"]:
        if not isinstance(raw_case, dict):
            raise EvidenceError("report contains an invalid case entry")
        case = raw_case.get("name")
        failures = raw_case.get("failures")
        if not isinstance(case, str) or not case or not isinstance(failures, list):
            raise EvidenceError("report contains an invalid case entry")
        for failure in failures:
            if not isinstance(failure, dict):
                raise EvidenceError("report contains an invalid failure entry")
            artifact = failure.get("artifact")
            signature = failure.get("finding_signature")
            if (
                failure.get("status") != Status.FAILED.value
                or not isinstance(artifact, str)
                or not isinstance(signature, str)
            ):
                raise EvidenceError(
                    f"case {case!r} contains unsigned or non-replayable failure evidence"
                )
            try:
                entry = EvidenceArtifactResult(
                    case=case,
                    artifact=artifact,
                    status=EvidenceArtifactStatus.ERROR,
                    expected_signature=signature,
                )
            except ValueError as exc:
                raise EvidenceError(f"case {case!r} contains invalid artifact evidence") from exc
            marker = (entry.case, entry.artifact, entry.expected_signature)
            binding = (entry.case, entry.expected_signature)
            previous = artifact_bindings.setdefault(entry.artifact, binding)
            if previous != binding:
                raise EvidenceError(
                    "one report artifact cannot be bound to conflicting case evidence"
                )
            if marker not in seen:
                entries.append(marker)
                seen.add(marker)
    if not entries:
        raise EvidenceError("report does not reference any signed counterexample artifacts")
    return entries


def _reported_artifact_root(entries: list[tuple[str, str, str]]) -> str:
    root_names: set[str] = set()
    for _case, value, _signature in entries:
        parts = Path(value).parts
        if len(parts) != 3 or parts[0] in {"", "."}:
            raise EvidenceError("report artifact paths must use artifact/case/campaign form")
        root_names.add(parts[0])
    if len(root_names) != 1:
        raise EvidenceError("one evidence report must reference one artifact directory")
    return next(iter(root_names))


def _resolve_artifact_root(
    entries: list[tuple[str, str, str]],
    artifact_root: str | Path | None,
) -> Path:
    reported_name = _reported_artifact_root(entries)
    declared = Path.cwd() / reported_name if artifact_root is None else Path(artifact_root)
    if declared.name != reported_name:
        raise EvidenceError("artifact root name does not match the report")
    if declared.is_symlink():
        raise EvidenceError("artifact root cannot be a symbolic link")
    try:
        resolved = declared.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError("artifact root is missing or invalid") from exc
    if not resolved.is_dir():
        raise EvidenceError("artifact root must be an existing directory")
    return resolved


def _contained_artifact(artifact_root: Path, value: str) -> Path:
    relative = Path(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 3
        or relative.parts[0] != artifact_root.name
    ):
        raise EvidenceError("artifact report paths must be relative and contained")
    try:
        resolved = artifact_root.joinpath(*relative.parts[1:]).resolve(strict=True)
    except OSError as exc:
        raise EvidenceError("a report-referenced artifact is missing") from exc
    if not resolved.is_relative_to(artifact_root):
        raise EvidenceError("artifact report paths must stay inside the artifact root")
    return resolved


def _stored_finding(artifact: Path) -> ExampleResult:
    # Reuse the replay manifest verifier so evidence verification and exact
    # replay share the same regular-file, size and SHA-256 integrity contract.
    from parity.engine import _artifact_root, _verify_manifest

    root = _artifact_root(artifact)
    _verify_manifest(root)
    try:
        payload = json.loads((root / "result.json").read_text(encoding="utf-8"))
        finding = ExampleResult.model_validate(payload)
    except (OSError, ValueError) as exc:
        raise EvidenceError("artifact result evidence is missing or invalid") from exc
    if finding.status is not Status.FAILED or finding.finding_signature is None:
        raise EvidenceError("artifact does not contain one signed semantic finding")
    return finding


def _actual_signature(result: Any, expected: str) -> tuple[Status, str | None]:
    if result.status is Status.ERROR:
        return Status.ERROR, None
    signatures = sorted(
        {
            failure.finding_signature
            for case in result.cases
            for failure in case.failures
            if failure.finding_signature is not None
        }
    )
    if result.status is Status.FAILED and signatures == [expected]:
        return Status.PASSED, expected
    different = next((signature for signature in signatures if signature != expected), None)
    return Status.FAILED, different


def _verify_one(
    *,
    case: str,
    artifact_name: str,
    expected_signature: str,
    artifact_root: Path,
) -> EvidenceArtifactResult:
    from parity.engine import replay_artifact

    try:
        artifact = _contained_artifact(artifact_root, artifact_name)
        stored = _stored_finding(artifact)
        if stored.finding_signature != expected_signature:
            raise EvidenceError("report signature does not match its artifact result")
        replayed = replay_artifact(artifact)
        if len(replayed.cases) != 1 or replayed.cases[0].name != case:
            raise EvidenceError("artifact case identity does not match its report entry")
        replay_case = replayed.cases[0]
        provenance = replay_case.provenance
        if (
            provenance is None
            or provenance.verification != "verified"
            or provenance.reference is None
            or provenance.candidate is None
        ):
            raise EvidenceError("artifact runtime provenance was not verified")
        replay_status, actual_signature = _actual_signature(replayed, expected_signature)
    except Exception:
        return EvidenceArtifactResult(
            case=case,
            artifact=redact_text(artifact_name),
            status=EvidenceArtifactStatus.ERROR,
            expected_signature=expected_signature,
        )
    status = (
        EvidenceArtifactStatus.VERIFIED
        if replay_status is Status.PASSED
        else EvidenceArtifactStatus.ERROR
        if replay_status is Status.ERROR
        else EvidenceArtifactStatus.STALE
    )
    return EvidenceArtifactResult(
        case=case,
        artifact=redact_text(artifact_name),
        status=status,
        expected_signature=expected_signature,
        actual_signature=actual_signature,
    )


def _result_status(artifacts: list[EvidenceArtifactResult]) -> Status:
    statuses = {artifact.status for artifact in artifacts}
    if EvidenceArtifactStatus.ERROR in statuses:
        return Status.ERROR
    if EvidenceArtifactStatus.STALE in statuses:
        return Status.FAILED
    return Status.PASSED


def verify_evidence(
    report: str | Path,
    *,
    artifact_root: str | Path | None = None,
) -> EvidenceResult:
    """Verify every signed counterexample referenced by a safe Parity report."""

    report_path = Path(report)
    try:
        raw = report_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise EvidenceError("evidence report is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("evidence report must contain a JSON object")
    entries = _report_entries(payload)
    root = _resolve_artifact_root(entries, artifact_root)
    artifacts = [
        _verify_one(
            case=case,
            artifact_name=artifact,
            expected_signature=signature,
            artifact_root=root,
        )
        for case, artifact, signature in entries
    ]
    return EvidenceResult(
        status=_result_status(artifacts),
        report_sha256=hashlib.sha256(raw).hexdigest(),
        artifacts=artifacts,
    )


def evidence_summary(result: EvidenceResult) -> dict[str, int]:
    """Return derived artifact counts for terminal and JSON reports."""

    counts = Counter(artifact.status for artifact in result.artifacts)
    return {
        "total": len(result.artifacts),
        "verified": counts[EvidenceArtifactStatus.VERIFIED],
        "stale": counts[EvidenceArtifactStatus.STALE],
        "error": counts[EvidenceArtifactStatus.ERROR],
    }


def evidence_report_payload(result: EvidenceResult) -> dict[str, Any]:
    """Return the data-safe evidence-verification report schema."""

    return {
        "schema_version": 1,
        "status": result.status.value,
        "parity_version": result.parity_version,
        "report_sha256": result.report_sha256,
        "summary": evidence_summary(result),
        "artifacts": [artifact.model_dump(mode="json") for artifact in result.artifacts],
    }


def render_evidence_json(result: EvidenceResult, *, pretty: bool = True) -> str:
    """Render data-safe evidence verification JSON."""

    return json.dumps(
        evidence_report_payload(result),
        indent=2 if pretty else None,
        sort_keys=pretty,
        allow_nan=False,
        separators=None if pretty else (",", ":"),
    ) + ("\n" if pretty else "")


def write_evidence_json(result: EvidenceResult, destination: str | Path) -> Path:
    """Atomically write evidence verification JSON."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(render_evidence_json(result))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "EvidenceArtifactResult",
    "EvidenceArtifactStatus",
    "EvidenceError",
    "EvidenceResult",
    "evidence_report_payload",
    "evidence_summary",
    "render_evidence_json",
    "verify_evidence",
    "write_evidence_json",
]
