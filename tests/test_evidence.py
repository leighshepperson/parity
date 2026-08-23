from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from parity import cli
from parity.evidence import (
    EvidenceArtifactReason,
    EvidenceArtifactResult,
    EvidenceArtifactStatus,
    EvidenceError,
    EvidenceResult,
    evidence_report_payload,
    verify_evidence,
    write_evidence_json,
)
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseProvenance,
    CaseResult,
    ExampleResult,
    FrameArgument,
    GenerationConfig,
    InvocationConfig,
    ParityConfig,
    PerformanceConfig,
    Status,
    SuiteResult,
)
from parity.provenance import collect_runtime_provenance
from parity.reporting import report_payload

SIGNATURE = "ms3:" + "a" * 64
runner = CliRunner()


def _write_artifact(root: Path, *, signature: str = SIGNATURE) -> Path:
    root.mkdir(parents=True)
    result = ExampleResult(
        source="fixture",
        status=Status.FAILED,
        finding_signature=signature,
    ).model_dump_json(indent=2)
    files = {
        "input-000.arrow": b"arrow",
        "replay.json": b"{}",
        "result.json": result.encode(),
    }
    manifest_files: dict[str, dict[str, object]] = {}
    for name, content in files.items():
        (root / name).write_bytes(content)
        manifest_files[name] = {
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    (root / "manifest.json").write_text(
        json.dumps({"version": 3, "files": manifest_files}), encoding="utf-8"
    )
    return root


def _report(
    artifact: str, *, migration: bool = False, signature: str = SIGNATURE
) -> dict[str, object]:
    suite: dict[str, object] = {
        "schema_version": 4,
        "status": "failed",
        "cases": [
            {
                "name": "orders",
                "failures": [
                    {
                        "status": "failed",
                        "artifact": artifact,
                        "finding_signature": signature,
                    }
                ],
            }
        ],
    }
    if migration:
        return {"schema_version": 1, "status": "failed", "parity": suite}
    return suite


def _replay(
    status: Status,
    signature: str | None = SIGNATURE,
    *,
    verification: Literal["captured", "verified", "drifted"] = "verified",
) -> SuiteResult:
    failures = (
        [
            ExampleResult(
                source="fixture",
                status=Status.FAILED,
                finding_signature=signature,
            )
        ]
        if status is Status.FAILED
        else []
    )
    runtime = collect_runtime_provenance()
    return SuiteResult(
        status=status,
        cases=[
            CaseResult(
                name="orders",
                status=status,
                examples_run=1,
                failures=failures,
                provenance=CaseProvenance(
                    reference=runtime,
                    candidate=runtime,
                    verification=verification,
                ),
            )
        ],
    )


@pytest.mark.parametrize("migration", [False, True])
def test_verifies_suite_and_migration_report_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration: bool
) -> None:
    artifact = _write_artifact(tmp_path / ".parity" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_report(str(artifact.relative_to(tmp_path)), migration=migration)),
        encoding="utf-8",
    )
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: _replay(Status.FAILED))

    result = verify_evidence(report, artifact_root=tmp_path / ".parity")

    assert result.status is Status.PASSED
    assert result.artifacts[0].status is EvidenceArtifactStatus.VERIFIED
    assert result.artifacts[0].actual_signature == SIGNATURE
    payload = evidence_report_payload(result)
    assert payload["summary"] == {"total": 1, "verified": 1, "stale": 0, "error": 0}
    assert payload["parity_version"]
    assert str(tmp_path) not in json.dumps(payload)


@pytest.mark.parametrize(
    ("replay", "expected_status", "artifact_status", "reason_code"),
    [
        (
            _replay(Status.PASSED),
            Status.FAILED,
            EvidenceArtifactStatus.STALE,
            EvidenceArtifactReason.FINDING_NOT_REPRODUCED,
        ),
        (
            _replay(Status.FAILED, "ms3:" + "b" * 64),
            Status.FAILED,
            EvidenceArtifactStatus.STALE,
            EvidenceArtifactReason.FINDING_CHANGED,
        ),
        (
            _replay(Status.ERROR, None),
            Status.ERROR,
            EvidenceArtifactStatus.ERROR,
            EvidenceArtifactReason.REPLAY_ERROR,
        ),
    ],
)
def test_distinguishes_stale_and_error_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replay: SuiteResult,
    expected_status: Status,
    artifact_status: EvidenceArtifactStatus,
    reason_code: EvidenceArtifactReason,
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(str(artifact.relative_to(tmp_path)))), encoding="utf-8")
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: replay)

    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")

    assert result.status is expected_status
    assert result.artifacts[0].status is artifact_status
    assert result.artifacts[0].reason_code is reason_code


def test_tampered_report_signature_is_an_error_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_report(str(artifact.relative_to(tmp_path)), signature="ms3:" + "b" * 64)),
        encoding="utf-8",
    )
    called = False

    def unexpected_replay(_artifact: Path) -> SuiteResult:
        nonlocal called
        called = True
        return _replay(Status.FAILED)

    monkeypatch.setattr("parity.engine.replay_artifact", unexpected_replay)

    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")

    assert result.status is Status.ERROR
    assert not called
    assert result.artifacts[0].reason_code is EvidenceArtifactReason.SIGNATURE_MISMATCH


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2},
        {"schema_version": True, "parity": {"schema_version": 4, "cases": []}},
        {"schema_version": 4.0, "cases": []},
        {"schema_version": 4, "cases": []},
        {
            "schema_version": 4,
            "cases": [{"name": "orders", "failures": [{"status": "failed"}]}],
        },
    ],
)
def test_rejects_unsupported_or_unsigned_reports(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceError):
        verify_evidence(report, artifact_root=tmp_path / "artifacts")


def test_rejects_artifact_traversal_and_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    _write_artifact(outside / "orders" / "finding")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report("../outside/finding")), encoding="utf-8")
    with pytest.raises(EvidenceError):
        verify_evidence(report, artifact_root=tmp_path / "escape")

    report.write_text(json.dumps(_report("escape/orders/finding")), encoding="utf-8")
    with pytest.raises(EvidenceError, match="symbolic link"):
        verify_evidence(report, artifact_root=tmp_path / "escape")

    artifact_root = tmp_path / "artifacts"
    (artifact_root / "orders").mkdir(parents=True)
    (artifact_root / "orders" / "finding").symlink_to(
        outside / "orders" / "finding", target_is_directory=True
    )
    report.write_text(json.dumps(_report("artifacts/orders/finding")), encoding="utf-8")
    result = verify_evidence(report, artifact_root=artifact_root)
    assert result.status is Status.ERROR
    assert result.artifacts[0].reason_code is EvidenceArtifactReason.ARTIFACT_UNAVAILABLE


def test_requires_verified_recorded_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(str(artifact.relative_to(tmp_path)))), encoding="utf-8")
    monkeypatch.setattr(
        "parity.engine.replay_artifact",
        lambda _artifact: _replay(Status.FAILED, verification="captured"),
    )

    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")

    assert result.status is Status.ERROR
    assert result.artifacts[0].status is EvidenceArtifactStatus.ERROR
    assert result.artifacts[0].reason_code is EvidenceArtifactReason.PROVENANCE_UNVERIFIED


def test_invalid_artifact_reports_a_bounded_reason_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    (artifact / "result.json").write_text("private corrupt detail", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(str(artifact.relative_to(tmp_path)))), encoding="utf-8")
    called = False

    def unexpected_replay(_artifact: Path) -> SuiteResult:
        nonlocal called
        called = True
        return _replay(Status.FAILED)

    monkeypatch.setattr("parity.engine.replay_artifact", unexpected_replay)

    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")

    assert not called
    assert result.artifacts[0].reason_code is EvidenceArtifactReason.ARTIFACT_INVALID
    assert "private corrupt detail" not in json.dumps(evidence_report_payload(result))


def test_case_identity_mismatch_reports_a_bounded_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(str(artifact.relative_to(tmp_path)))), encoding="utf-8")
    replay = _replay(Status.FAILED)
    replay.cases[0] = replay.cases[0].model_copy(update={"name": "different-case"})
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: replay)

    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")

    assert result.artifacts[0].reason_code is EvidenceArtifactReason.CASE_MISMATCH


@pytest.mark.parametrize(
    ("status", "actual"),
    [
        (EvidenceArtifactStatus.VERIFIED, None),
        (EvidenceArtifactStatus.VERIFIED, "ms3:" + "b" * 64),
        (EvidenceArtifactStatus.STALE, SIGNATURE),
        (EvidenceArtifactStatus.ERROR, "ms3:" + "b" * 64),
    ],
)
def test_artifact_result_rejects_contradictory_signatures(
    status: EvidenceArtifactStatus, actual: str | None
) -> None:
    with pytest.raises(ValidationError):
        EvidenceArtifactResult(
            case="orders",
            artifact="artifacts/orders/finding",
            status=status,
            expected_signature=SIGNATURE,
            actual_signature=actual,
        )


@pytest.mark.parametrize(
    ("status", "actual", "reason"),
    [
        (
            EvidenceArtifactStatus.VERIFIED,
            SIGNATURE,
            EvidenceArtifactReason.REPLAY_FAILED,
        ),
        (EvidenceArtifactStatus.STALE, None, None),
        (
            EvidenceArtifactStatus.STALE,
            None,
            EvidenceArtifactReason.REPLAY_FAILED,
        ),
        (
            EvidenceArtifactStatus.STALE,
            None,
            EvidenceArtifactReason.FINDING_CHANGED,
        ),
        (EvidenceArtifactStatus.ERROR, None, None),
        (
            EvidenceArtifactStatus.ERROR,
            None,
            EvidenceArtifactReason.FINDING_NOT_REPRODUCED,
        ),
    ],
)
def test_artifact_result_rejects_missing_or_contradictory_reason_codes(
    status: EvidenceArtifactStatus,
    actual: str | None,
    reason: EvidenceArtifactReason | None,
) -> None:
    with pytest.raises(ValidationError):
        EvidenceArtifactResult(
            case="orders",
            artifact="artifacts/orders/finding",
            status=status,
            expected_signature=SIGNATURE,
            actual_signature=actual,
            reason_code=reason,
        )


def test_one_replay_exception_does_not_abort_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _write_artifact(tmp_path / "artifacts" / "orders" / "first")
    second = _write_artifact(tmp_path / "artifacts" / "orders" / "second")
    payload = _report(str(first.relative_to(tmp_path)))
    cases = payload["cases"]
    assert isinstance(cases, list)
    failures = cases[0]["failures"]
    assert isinstance(failures, list)
    failures.append(
        {
            "status": "failed",
            "artifact": str(second.relative_to(tmp_path)),
            "finding_signature": SIGNATURE,
        }
    )
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    calls = 0

    def replay(_artifact: Path) -> SuiteResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private replay detail")
        return _replay(Status.FAILED)

    monkeypatch.setattr("parity.engine.replay_artifact", replay)

    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")

    assert calls == 2
    assert result.status is Status.ERROR
    assert [item.status for item in result.artifacts] == [
        EvidenceArtifactStatus.ERROR,
        EvidenceArtifactStatus.VERIFIED,
    ]
    assert result.artifacts[0].reason_code is EvidenceArtifactReason.REPLAY_FAILED
    assert "private replay detail" not in json.dumps(evidence_report_payload(result))


def test_report_deduplicates_in_order_and_rejects_conflicting_artifact_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    payload = _report(str(artifact.relative_to(tmp_path)))
    cases = payload["cases"]
    assert isinstance(cases, list)
    failures = cases[0]["failures"]
    assert isinstance(failures, list)
    failures.append(dict(failures[0]))
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    calls = 0

    def replay(_artifact: Path) -> SuiteResult:
        nonlocal calls
        calls += 1
        return _replay(Status.FAILED)

    monkeypatch.setattr("parity.engine.replay_artifact", replay)
    result = verify_evidence(report, artifact_root=tmp_path / "artifacts")
    assert calls == 1
    assert [item.artifact for item in result.artifacts] == ["artifacts/orders/finding"]

    failures.append(
        {
            "status": "failed",
            "artifact": str(artifact.relative_to(tmp_path)),
            "finding_signature": "ms3:" + "b" * 64,
        }
    )
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceError, match="conflicting case evidence"):
        verify_evidence(report, artifact_root=tmp_path / "artifacts")


def test_evidence_json_writer_preserves_destination_and_cleans_temporary_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = EvidenceResult(
        status=Status.PASSED,
        report_sha256="a" * 64,
        artifacts=[
            EvidenceArtifactResult(
                case="orders",
                artifact="artifacts/orders/finding",
                status=EvidenceArtifactStatus.VERIFIED,
                expected_signature=SIGNATURE,
                actual_signature=SIGNATURE,
            )
        ],
    )
    destination = tmp_path / "verification.json"
    destination.write_text("previous\n", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("private destination detail")

    monkeypatch.setattr("parity.evidence.os.replace", fail_replace)
    with pytest.raises(OSError, match="private destination detail"):
        write_evidence_json(result, destination)

    assert destination.read_text(encoding="utf-8") == "previous\n"
    assert list(tmp_path.glob(".verification.json.*")) == []


def test_evidence_json_writer_creates_missing_parent_directories(tmp_path: Path) -> None:
    result = EvidenceResult(
        status=Status.PASSED,
        report_sha256="a" * 64,
        artifacts=[
            EvidenceArtifactResult(
                case="orders",
                artifact="artifacts/orders/finding",
                status=EvidenceArtifactStatus.VERIFIED,
                expected_signature=SIGNATURE,
                actual_signature=SIGNATURE,
            )
        ],
    )

    destination = write_evidence_json(result, tmp_path / "nested" / "reports" / "evidence.json")

    assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "passed"
    assert list(destination.parent.glob(".evidence.json.*")) == []


def test_explicit_artifact_root_supports_nested_configured_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "state" / "private" / ".parity"
    artifact = _write_artifact(artifact_root / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(".parity/orders/finding")), encoding="utf-8")
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: _replay(Status.FAILED))

    result = verify_evidence(report, artifact_root=artifact_root)

    assert result.status is Status.PASSED
    assert artifact.is_dir()


def test_report_beneath_nested_artifact_root_is_self_locating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "migrations" / ".parity"
    artifact = _write_artifact(artifact_root / "orders" / "finding")
    report = artifact_root / "workspace" / "reports" / "default.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps(_report(".parity/orders/finding")), encoding="utf-8")
    # A same-named cwd root must not take precedence over the report's own root.
    (tmp_path / ".parity").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: _replay(Status.FAILED))

    result = verify_evidence(report)

    assert result.status is Status.PASSED
    assert result.artifacts[0].status is EvidenceArtifactStatus.VERIFIED
    assert artifact.is_dir()


def test_self_locating_report_does_not_follow_an_artifact_root_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-inferred-root"
    outside.mkdir()
    artifact_root = tmp_path / ".parity"
    artifact_root.symlink_to(outside, target_is_directory=True)
    report = artifact_root / "report.json"
    report.write_text(json.dumps(_report(".parity/orders/finding")), encoding="utf-8")

    with pytest.raises(EvidenceError, match="symbolic link"):
        verify_evidence(report)


@pytest.mark.parametrize(
    ("replay", "exit_code", "label"),
    [
        (_replay(Status.FAILED), 0, "verified"),
        (_replay(Status.PASSED), 1, "stale"),
        (_replay(Status.ERROR, None), 2, "error"),
    ],
)
def test_evidence_cli_exit_contract_and_safe_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replay: SuiteResult,
    exit_code: int,
    label: str,
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(str(artifact.relative_to(tmp_path)))), encoding="utf-8")
    output = tmp_path / "verification.json"
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: replay)

    completed = runner.invoke(
        cli.app,
        [
            "evidence",
            "verify",
            str(report),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--json",
            str(output),
        ],
    )

    assert completed.exit_code == exit_code, completed.output
    assert label in completed.output
    if exit_code == 1:
        assert "finding_not_reproduced" in completed.output
    elif exit_code == 2:
        assert "replay_error" in completed.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == {0: "passed", 1: "failed", 2: "error"}[exit_code]
    assert (
        payload["artifacts"][0]["reason_code"]
        == {
            0: None,
            1: "finding_not_reproduced",
            2: "replay_error",
        }[exit_code]
    )
    assert str(tmp_path) not in output.read_text(encoding="utf-8")
    assert str(tmp_path) not in completed.output


def test_evidence_cli_json_write_failure_is_operational_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _write_artifact(tmp_path / "artifacts" / "orders" / "finding")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(str(artifact.relative_to(tmp_path)))), encoding="utf-8")
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    monkeypatch.setattr("parity.engine.replay_artifact", lambda _artifact: _replay(Status.FAILED))

    completed = runner.invoke(
        cli.app,
        [
            "evidence",
            "verify",
            str(report),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--json",
            str(output_directory),
        ],
    )

    assert completed.exit_code == 2
    assert "evidence report could not be written" in completed.output
    assert "verified" not in completed.output


def test_real_report_artifact_replays_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parity.engine import run_suite

    (tmp_path / "transforms.py").write_text(
        """
def reference(frame):
    return frame

def candidate(frame):
    return frame.append_column("extra", frame.column(0))
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    table = pa.table({"id": [1]})
    with pa.OSFile(str(fixture), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="orders",
        reference=CallableSpec(target="transforms:reference", adapter="arrow", workdir=tmp_path),
        candidate=CallableSpec(target="transforms:candidate", adapter="arrow", workdir=tmp_path),
        invocation=InvocationConfig(args=[FrameArgument(fixture=fixture)]),
        generation=GenerationConfig(
            max_examples=1,
            adversarial_examples=False,
            search=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )
    suite = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))
    assert suite.status is Status.FAILED
    report = tmp_path / "report.json"
    report.write_text(json.dumps(report_payload(suite)), encoding="utf-8")

    verified = verify_evidence(report)

    assert verified.status is Status.PASSED
    assert verified.artifacts[0].status is EvidenceArtifactStatus.VERIFIED
