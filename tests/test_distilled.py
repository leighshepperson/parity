from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from parity import cli
from parity.distilled import (
    ContractError,
    DistilledContractManifest,
    distill_contract,
    verify_contract,
)
from parity.engine import run_suite
from parity.models import (
    CallableSpec,
    CaseConfig,
    GenerationConfig,
    InputBundle,
    InputSpec,
    ParityConfig,
    PerformanceConfig,
    Status,
)
from parity.reporting import write_report

runner = CliRunner()


def _write_arrow(path: Path) -> None:
    table = pa.table({"value": [1, 5]})
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)


def _clear_import_cache(project: Path) -> None:
    shutil.rmtree(project / "__pycache__", ignore_errors=True)


def _finding_report(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: str | None = None,
) -> Path:
    module = project / "upgrade_target.py"
    module.write_text(
        source
        or """
def reference(table):
    return table.column("value")[0].as_py() + 1

def candidate(table):
    return table.column("value")[0].as_py()
""",
        encoding="utf-8",
    )
    fixture = project / "input.arrow"
    _write_arrow(fixture)
    monkeypatch.chdir(project)
    case = CaseConfig(
        name="upgrade",
        reference=CallableSpec(
            target="upgrade_target:reference",
            adapter="arrow",
            workdir=project,
        ),
        candidate=CallableSpec(
            target="upgrade_target:candidate",
            adapter="arrow",
            workdir=project,
        ),
        fixture=fixture,
        generation=GenerationConfig(
            search=False,
            adversarial_examples=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )
    result = run_suite(ParityConfig(artifact_dir=project / ".parity", cases=[case]))
    assert result.status is Status.FAILED
    report = project / ".parity" / "report.json"
    write_report(result, "json", report)
    return report


@pytest.mark.integration
def test_distilled_contract_verifies_after_reference_is_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    destination = tmp_path / ".parity-contract"

    distilled = distill_contract(report, destination)

    assert distilled.cases == 1
    assert distilled.examples == 1
    assert (destination / ".gitignore").read_text(encoding="utf-8") == "*\n"
    manifest = DistilledContractManifest.model_validate_json(
        (destination / "contract.json").read_text(encoding="utf-8")
    )
    assert manifest.cases[0].candidate.target == "upgrade_target:candidate"
    assert all("reference" not in key for key in manifest.cases[0].model_dump())

    (tmp_path / "upgrade_target.py").write_text(
        """
def candidate(table):
    return table.column("value")[0].as_py() + 1
""",
        encoding="utf-8",
    )
    _clear_import_cache(tmp_path)

    result = verify_contract(destination)

    assert result.status is Status.PASSED
    assert result.cases[0].examples_run == 1
    assert result.cases[0].failures == []


@pytest.mark.integration
def test_contract_reports_candidate_mismatch_and_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    destination = tmp_path / "contract"
    distill_contract(report, destination)

    mismatch = verify_contract(destination)
    assert mismatch.status is Status.FAILED
    assert mismatch.cases[0].failures[0].finding_signature is not None

    (tmp_path / "upgrade_target.py").write_text(
        """
import os

def candidate(table):
    os._exit(23)
""",
        encoding="utf-8",
    )
    _clear_import_cache(tmp_path)
    crashed = verify_contract(destination)

    assert crashed.status is Status.ERROR
    assert crashed.cases[0].failures[0].status is Status.ERROR


@pytest.mark.integration
def test_contract_preserves_reference_exception_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(
        tmp_path,
        monkeypatch,
        source="""
def reference(table):
    raise ValueError("bad row 41")

def candidate(table):
    raise TypeError("bad row 99")
""",
    )
    destination = tmp_path / "contract"
    distill_contract(report, destination)
    (tmp_path / "upgrade_target.py").write_text(
        """
def candidate(table):
    raise ValueError("bad row 812")
""",
        encoding="utf-8",
    )
    _clear_import_cache(tmp_path)

    result = verify_contract(destination)

    assert result.status is Status.PASSED


@pytest.mark.integration
def test_contract_preserves_arrow_reference_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(
        tmp_path,
        monkeypatch,
        source="""
def reference(table):
    return table.append_column("expected", table.column("value"))

def candidate(table):
    return table
""",
    )
    destination = tmp_path / "contract"
    distill_contract(report, destination)
    manifest = DistilledContractManifest.model_validate_json(
        (destination / "contract.json").read_text(encoding="utf-8")
    )
    assert manifest.cases[0].examples[0].expected.output is not None
    assert manifest.cases[0].examples[0].expected.output.kind == "arrow"
    (tmp_path / "upgrade_target.py").write_text(
        """
def candidate(table):
    return table.append_column("expected", table.column("value"))
""",
        encoding="utf-8",
    )
    _clear_import_cache(tmp_path)

    assert verify_contract(destination).status is Status.PASSED


@pytest.mark.integration
def test_contract_preserves_positional_multi_input_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "upgrade_target.py").write_text(
        """
def reference(left, right):
    return left.column("value")[0].as_py() + right.column("value")[0].as_py()

def candidate(left, right):
    return left.column("value")[0].as_py()
""",
        encoding="utf-8",
    )
    left = tmp_path / "left.arrow"
    right = tmp_path / "right.arrow"
    _write_arrow(left)
    _write_arrow(right)
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="bundle",
        reference=CallableSpec(
            target="upgrade_target:reference", adapter="arrow", workdir=tmp_path
        ),
        candidate=CallableSpec(
            target="upgrade_target:candidate", adapter="arrow", workdir=tmp_path
        ),
        input_bundle=InputBundle(
            binding="positional",
            inputs={
                "left": InputSpec(fixture=left),
                "right": InputSpec(fixture=right),
            },
        ),
        generation=GenerationConfig(
            search=False,
            adversarial_examples=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )
    checked = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))
    assert checked.status is Status.FAILED
    report = tmp_path / ".parity" / "report.json"
    write_report(checked, "json", report)
    destination = tmp_path / "contract"
    distill_contract(report, destination)
    (tmp_path / "upgrade_target.py").write_text(
        """
def candidate(left, right):
    return left.column("value")[0].as_py() + right.column("value")[0].as_py()
""",
        encoding="utf-8",
    )
    _clear_import_cache(tmp_path)

    result = verify_contract(destination)

    assert result.status is Status.PASSED
    manifest = DistilledContractManifest.model_validate_json(
        (destination / "contract.json").read_text(encoding="utf-8")
    )
    assert manifest.cases[0].input_binding == "positional"
    assert [item.name for item in manifest.cases[0].examples[0].inputs] == ["left", "right"]


def test_contract_integrity_rejects_tampered_private_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    destination = tmp_path / "contract"
    distill_contract(report, destination)
    manifest = DistilledContractManifest.model_validate_json(
        (destination / "contract.json").read_text(encoding="utf-8")
    )
    target = destination / next(iter(manifest.files))
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(ContractError, match="size check failed"):
        verify_contract(destination)


def test_distillation_rejects_pre_contract_artifacts_with_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    payload = json.loads(report.read_text(encoding="utf-8"))
    artifact = tmp_path / payload["cases"][0]["failures"][0]["artifact"]
    artifact_manifest_path = artifact / "manifest.json"
    artifact_manifest = json.loads(artifact_manifest_path.read_text(encoding="utf-8"))
    for name in list(artifact_manifest["files"]):
        if name.startswith("reference"):
            (artifact / name).unlink()
            artifact_manifest["files"].pop(name)
    artifact_manifest_path.write_text(
        json.dumps(artifact_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="rerun parity check"):
        distill_contract(report, tmp_path / "contract")


def test_distillation_requires_a_new_destination_inside_the_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ContractError, match="already exists"):
        distill_contract(report, existing)

    outside = tmp_path.parent / f"{tmp_path.name}-outside-contract"
    with pytest.raises(ContractError, match="inside the recorded project root"):
        distill_contract(report, outside)
    assert not outside.exists()


@pytest.mark.integration
def test_contract_cli_distills_verifies_and_writes_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    destination = tmp_path / "contract"

    distilled = runner.invoke(
        cli.app,
        ["contract", "distill", str(report), str(destination)],
    )
    assert distilled.exit_code == 0, distilled.output
    assert "1 example(s) across 1 case(s)" in distilled.output

    (tmp_path / "upgrade_target.py").write_text(
        """
def candidate(table):
    return table.column("value")[0].as_py() + 1
""",
        encoding="utf-8",
    )
    _clear_import_cache(tmp_path)
    output = tmp_path / "verification.json"
    verified = runner.invoke(
        cli.app,
        ["contract", "verify", str(destination), "--json", str(output)],
    )

    assert verified.exit_code == 0, verified.output
    assert "PASSED" in verified.output
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


def test_contract_manifest_binds_every_private_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _finding_report(tmp_path, monkeypatch)
    destination = tmp_path / "contract"
    distill_contract(report, destination)
    payload = json.loads((destination / "contract.json").read_text(encoding="utf-8"))

    referenced = {
        item["file"]
        for case in payload["cases"]
        for example in case["examples"]
        for item in example["inputs"]
    }
    referenced.update(
        example["expected"]["output"]["file"]
        for case in payload["cases"]
        for example in case["examples"]
        if example["expected"]["output"] is not None
    )
    assert referenced == set(payload["files"])
    for name, metadata in payload["files"].items():
        content = (destination / name).read_bytes()
        assert len(content) == metadata["bytes"]
        assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
