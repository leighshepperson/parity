"""Reviewable compatibility budgets for intentionally accepted differences."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field

from parity.models import (
    CompatibilityBudget,
    CompatibilityDecision,
    CompatibilityFinding,
    StrictModel,
)


class CompatibilityBudgetError(ValueError):
    """Raised when a compatibility budget cannot be created or trusted."""


class BudgetCaptureResult(StrictModel):
    """Summary returned after creating a review budget from one report."""

    path: Path
    findings: int = Field(ge=1)
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _atomic_write(path: Path, content: str, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            if os.path.lexists(path) and not (path.is_file() or path.is_symlink()):
                raise CompatibilityBudgetError("compatibility budget destination is not a file")
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise FileExistsError(f"compatibility budget already exists: {path}") from None
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def render_compatibility_budget(budget: CompatibilityBudget) -> str:
    """Render one deterministic, review-friendly compatibility TOML document."""

    lines = [
        "# compatibility.toml — review every difference before approval",
        "version = 1",
        f"source_report_sha256 = {json.dumps(budget.source_report_sha256)}",
        "",
    ]
    for finding in budget.findings:
        lines.extend(
            [
                "[[findings]]",
                f"case = {json.dumps(finding.case)}",
                f"finding_signature = {json.dumps(finding.finding_signature)}",
                f"decision = {json.dumps(finding.decision.value)}",
            ]
        )
        if finding.reason is not None:
            lines.append(f"reason = {json.dumps(finding.reason)}")
        lines.append("")
    return "\n".join(lines)


def load_compatibility_budget(path: str | Path) -> CompatibilityBudget:
    """Load and strictly validate one compatibility budget."""

    budget_path = Path(path)
    try:
        raw = tomllib.loads(budget_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompatibilityBudgetError(f"compatibility budget not found: {budget_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CompatibilityBudgetError(
            f"invalid TOML in compatibility budget {budget_path}: {exc}"
        ) from exc
    try:
        return CompatibilityBudget.model_validate(raw)
    except ValueError as exc:
        raise CompatibilityBudgetError(f"invalid compatibility budget: {exc}") from exc


def _suite_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema_version = payload.get("schema_version")
    if type(schema_version) is int and schema_version == 4:
        suite = payload
    elif (
        type(schema_version) is int
        and schema_version == 1
        and isinstance(payload.get("parity"), dict)
    ):
        suite = payload["parity"]
    else:
        raise CompatibilityBudgetError("report must be Parity suite schema 4 or migration schema 1")
    if type(suite.get("schema_version")) is not int or suite.get("schema_version") != 4:
        raise CompatibilityBudgetError("report contains an unsupported Parity result payload")
    return suite


def _report_findings(payload: dict[str, Any]) -> list[CompatibilityFinding]:
    from parity.json_contracts import SuiteReportContract

    try:
        suite = SuiteReportContract.model_validate(_suite_payload(payload))
    except ValueError as exc:
        raise CompatibilityBudgetError("report contains an invalid Parity result payload") from exc
    findings: list[CompatibilityFinding] = []
    for case in suite.cases:
        for failure in case.failures:
            if failure.finding_signature is None:
                raise CompatibilityBudgetError(
                    f"case {case.name!r} contains an operational or unsigned failure; "
                    "compatibility budgets can approve only signed semantic findings"
                )
            findings.append(
                CompatibilityFinding(
                    case=case.name,
                    finding_signature=failure.finding_signature,
                )
            )
    unique = {(finding.case, finding.finding_signature): finding for finding in findings}
    if not unique:
        raise CompatibilityBudgetError("report contains no signed findings to review")
    return [unique[key] for key in sorted(unique)]


def capture_compatibility_budget(
    report: str | Path,
    destination: str | Path,
    *,
    force: bool = False,
) -> BudgetCaptureResult:
    """Create a review-state budget from every signed finding in a safe report."""

    report_path = Path(report)
    try:
        raw = report_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise CompatibilityBudgetError("compatibility report is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise CompatibilityBudgetError("compatibility report must contain a JSON object")
    digest = hashlib.sha256(raw).hexdigest()
    budget = CompatibilityBudget(
        source_report_sha256=digest,
        findings=_report_findings(payload),
    )
    path = Path(destination)
    _atomic_write(path, render_compatibility_budget(budget), force=force)
    return BudgetCaptureResult(
        path=path,
        findings=len(budget.findings),
        source_report_sha256=digest,
    )


def approve_compatibility_finding(
    budget: str | Path,
    case: str,
    finding_signature: str,
    *,
    reason: str,
) -> CompatibilityBudget:
    """Atomically approve one report-captured, case-scoped finding with rationale."""

    path = Path(budget)
    current = load_compatibility_budget(path)
    identity = (case, finding_signature)
    if identity not in {(finding.case, finding.finding_signature) for finding in current.findings}:
        raise CompatibilityBudgetError(
            "finding is not present in this budget; recapture from the current report"
        )
    updated = current.model_copy(
        update={
            "findings": [
                finding.model_copy(
                    update={"decision": CompatibilityDecision.APPROVED, "reason": reason}
                )
                if (finding.case, finding.finding_signature) == identity
                else finding
                for finding in current.findings
            ]
        }
    )
    try:
        updated = CompatibilityBudget.model_validate(updated.model_dump(mode="python"))
    except ValueError as exc:
        raise CompatibilityBudgetError(f"finding approval is invalid: {exc}") from exc
    _atomic_write(path, render_compatibility_budget(updated), force=True)
    return updated


__all__ = [
    "BudgetCaptureResult",
    "CompatibilityBudgetError",
    "approve_compatibility_finding",
    "capture_compatibility_budget",
    "load_compatibility_budget",
    "render_compatibility_budget",
]
