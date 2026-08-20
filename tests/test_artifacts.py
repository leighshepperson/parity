from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from parity.artifacts import ArtifactStore
from parity.engine import ReplayError, replay_artifact
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseProvenance,
    ComparisonPolicy,
    ExampleResult,
    InputBundle,
    InputSpec,
    Mismatch,
    MismatchKind,
    Status,
)
from parity.provenance import collect_runtime_provenance, effective_config_sha256


def _case(tmp_path: Path) -> CaseConfig:
    return CaseConfig(
        name="orders",
        reference=CallableSpec(
            target="old:transform",
            adapter="pandas",
            workdir=tmp_path / "private-workdir",
            environment={"API_TOKEN": "do-not-store"},
        ),
        candidate=CallableSpec(target="new:transform", adapter="polars"),
        fixture=tmp_path / "source.parquet",
        static_kwargs={"api_key": "also-secret", "mode": "strict"},
        reference_kwargs={"engine": "pandas", "reference_token": "reference-secret"},
        candidate_kwargs={"engine": "polars", "candidate_token": "candidate-secret"},
    )


def _result() -> ExampleResult:
    return ExampleResult(
        source="generated",
        status=Status.FAILED,
        mismatches=[
            Mismatch(
                kind=MismatchKind.VALUE,
                message="cell differs",
                reference="customer-a",
                candidate="customer-b",
            )
        ],
    )


def test_inspection_artifact_is_complete_hashed_and_not_claimed_as_replayable(
    tmp_path: Path,
) -> None:
    destination = ArtifactStore(tmp_path / "artifacts").write_failure(
        _case(tmp_path),
        pa.table({"account": ["customer-a"], "amount": [10]}),
        _result(),
        source="generated",
        seed=17,
    )
    expected = {"input.arrow", "input.parquet", "result.json", "replay.json", "manifest.json"}
    assert {path.name for path in destination.iterdir()} == expected
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["case"] == "orders"
    assert manifest["seed"] == 17
    assert manifest["contains_input_data"] is True
    for name, metadata in manifest["files"].items():
        content = (destination / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
        assert len(content) == metadata["bytes"]
    replay_text = (destination / "replay.json").read_text(encoding="utf-8")
    replay = json.loads(replay_text)
    assert replay["version"] == 2
    assert "expected_runtime" not in replay
    assert "config_sha256" not in replay
    assert "command" not in replay
    assert "path_base" not in replay
    assert replay["inputs"] == [{"name": "input", "file": "input.arrow"}]
    assert replay["case"]["fixture"] == "input.arrow"
    assert replay["case"]["reference"] is None
    assert replay["replay_blockers"] == {"reference": "external_workdir"}
    assert replay["case"]["static_kwargs"]["api_key"] == "<redacted>"
    assert replay["case"]["static_kwargs"]["mode"] == "strict"
    assert replay["case"]["reference_kwargs"] == {
        "engine": "pandas",
        "reference_token": "<redacted>",
    }
    assert replay["case"]["candidate_kwargs"] == {
        "candidate_token": "<redacted>",
        "engine": "polars",
    }
    assert "do-not-store" not in replay_text
    assert "also-secret" not in replay_text
    assert "reference-secret" not in replay_text
    assert "candidate-secret" not in replay_text
    assert str(tmp_path) not in replay_text


def test_artifact_can_use_a_stable_project_root_from_an_unrelated_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    unrelated = tmp_path / "elsewhere"
    project.mkdir()
    unrelated.mkdir()
    reference_python = project / ".parity/workspace/envs/reference/bin/python"
    candidate_python = project / ".parity/workspace/envs/candidate/bin/python"
    for python in (reference_python, candidate_python):
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
    case = CaseConfig(
        name="stable-root",
        reference=CallableSpec(
            target="migration_adapters:reference",
            workdir=project,
            python=reference_python,
        ),
        candidate=CallableSpec(
            target="migration_adapters:candidate",
            workdir=project,
            python=candidate_python,
        ),
        fixture=project / "fixture.arrow",
    )
    runtime = collect_runtime_provenance()
    monkeypatch.chdir(unrelated)

    destination = ArtifactStore(
        project / ".parity/artifacts",
        invocation_directory=project,
    ).write_failure(
        case,
        pa.table({"id": [1]}),
        _result(),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="d" * 64,
    )

    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert "replay_blockers" not in replay
    assert replay["path_base"] == {"kind": "artifact_ancestor", "levels": 4}
    assert replay["case"]["reference"]["workdir"] == "."
    assert replay["case"]["candidate"]["workdir"] == "."
    assert replay["case"]["reference"]["python"] == (".parity/workspace/envs/reference/bin/python")
    assert replay["case"]["candidate"]["python"] == (".parity/workspace/envs/candidate/bin/python")
    assert replay["command"] == ["parity", "replay", "<artifact-path>"]
    assert str(unrelated) not in json.dumps(replay)


def test_replayable_artifact_outside_project_root_records_bounded_blocker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    artifact_root = tmp_path / "external-artifacts"
    project.mkdir()
    runtime = collect_runtime_provenance()
    case = CaseConfig(
        name="external-artifact",
        reference=CallableSpec(target="project:reference"),
        candidate=CallableSpec(target="project:candidate"),
        fixture=project / "fixture.arrow",
    )

    destination = ArtifactStore(
        artifact_root,
        invocation_directory=project,
    ).write_failure(
        case,
        pa.table({"id": [1]}),
        _result(),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="e" * 64,
    )

    replay_text = (destination / "replay.json").read_text(encoding="utf-8")
    replay = json.loads(replay_text)
    assert replay["replay_blockers"] == {"artifact": "external_artifact_root"}
    assert "path_base" not in replay
    assert "command" not in replay
    assert str(tmp_path) not in replay_text
    with pytest.raises(ReplayError, match="outside the recorded configuration directory"):
        replay_artifact(destination)


def test_artifact_root_is_self_ignoring_without_replacing_user_policy(tmp_path: Path) -> None:
    root = tmp_path / ".parity"
    ArtifactStore(root).write_failure("orders", pa.table({"id": [1]}), _result())
    assert (root / ".gitignore").read_text(encoding="utf-8") == "*\n"

    (root / ".gitignore").write_text("# user policy\n", encoding="utf-8")
    ArtifactStore(root).write_failure("customers", pa.table({"id": [2]}), _result())
    assert (root / ".gitignore").read_text(encoding="utf-8") == "# user policy\n"


def test_artifact_records_complete_runtime_and_config_bindings(
    tmp_path: Path,
) -> None:
    runtime = collect_runtime_provenance(["definitely-not-installed-artifact-probe"])
    case = CaseConfig(
        name="complete-runtime",
        reference=CallableSpec(
            target="old:transform",
            adapter="pandas",
            required_distributions={"numpy": ">=1"},
        ),
        candidate=CallableSpec(
            target="new:transform",
            adapter="polars",
            required_distributions={"numpy": ">=1"},
        ),
        fixture=tmp_path / "source.arrow",
        comparison=ComparisonPolicy(row_order="keyed", row_keys=["account", "sequence"]),
    )
    destination = ArtifactStore(
        tmp_path / "artifacts", invocation_directory=tmp_path
    ).write_failure(
        case,
        pa.table({"account": ["A"], "sequence": [1], "value": [10]}),
        _result(),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="a" * 64,
    )

    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert replay["version"] == 2
    assert replay["path_base"] == {"kind": "artifact_ancestor", "levels": 3}
    assert replay["command"] == ["parity", "replay", "<artifact-path>"]
    assert replay["config_sha256"] == "a" * 64
    assert replay["expected_runtime"]["reference"]["python_version"]
    assert replay["expected_runtime"]["candidate"]["distributions"]
    assert replay["case"]["comparison"]["row_order"] == "keyed"
    assert replay["case"]["comparison"]["row_keys"] == ["account", "sequence"]

    restored = CaseConfig.model_validate(replay["case"])
    assert restored.comparison == case.comparison
    assert restored.reference.required_distributions == {"numpy": ">=1"}
    assert restored.candidate.required_distributions == {"numpy": ">=1"}


def test_artifact_preserves_partial_runtime_for_inspection_without_replay_command(
    tmp_path: Path,
) -> None:
    runtime = collect_runtime_provenance()
    destination = ArtifactStore(
        tmp_path / "artifacts", invocation_directory=tmp_path
    ).write_failure(
        "partial-runtime",
        pa.table({"x": [1]}),
        _result(),
        reference=CallableSpec(target="project:reference"),
        candidate=CallableSpec(target="project:candidate"),
        runtime_provenance=CaseProvenance(reference=runtime),
        config_sha256="b" * 64,
    )

    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert replay["expected_runtime"]["reference"]["python_version"]
    assert replay["expected_runtime"]["candidate"] is None
    assert replay["config_sha256"] == "b" * 64
    assert "command" not in replay


def test_artifact_preserves_project_virtualenv_python_entrypoint(tmp_path: Path) -> None:
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    runtime = collect_runtime_provenance()
    case = CaseConfig(
        name="venv-replay",
        reference=CallableSpec(
            target="project.transform:run",
            adapter="arrow",
            python=interpreter,
            workdir=tmp_path,
        ),
        candidate=CallableSpec(
            target="project.transform:run",
            adapter="arrow",
            python=interpreter,
            workdir=tmp_path,
        ),
        fixture=tmp_path / "source.arrow",
    )

    config_sha256 = effective_config_sha256(
        {"version": 1, "cases": [case.model_dump(mode="python", by_alias=True)]}
    )
    alternate = case.model_copy(deep=True)
    alternate.candidate.python = tmp_path / ".venv-other" / "bin" / "python"
    alternate_hash = effective_config_sha256(
        {"version": 1, "cases": [alternate.model_dump(mode="python", by_alias=True)]}
    )
    assert config_sha256 != alternate_hash

    old_directory = Path.cwd()
    try:
        os.chdir(tmp_path)
        destination = ArtifactStore(tmp_path / "artifacts").write_failure(
            case,
            pa.table({"id": [1]}),
            _result(),
            runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
            config_sha256=config_sha256,
        )
    finally:
        os.chdir(old_directory)

    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert replay["case"]["reference"]["python"] == ".venv/bin/python"
    assert replay["case"]["candidate"]["python"] == ".venv/bin/python"
    assert replay["config_sha256"] == config_sha256


@pytest.mark.parametrize(
    ("field", "reason", "message"),
    [
        (
            "workdir",
            "external_workdir",
            "reference.workdir was outside the recorded configuration directory",
        ),
        (
            "python",
            "external_python",
            "reference.python was outside the recorded configuration directory",
        ),
    ],
)
def test_external_target_paths_record_an_actionable_replay_reason(
    tmp_path: Path,
    field: str,
    reason: str,
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    reference = (
        CallableSpec(target="project:reference", python=external / "bin" / "python")
        if field == "python"
        else CallableSpec(target="project:reference", workdir=external)
    )
    runtime = collect_runtime_provenance()
    case = CaseConfig(
        name=f"external-{field}",
        reference=reference,
        candidate=CallableSpec(target="project:candidate"),
        fixture=project / "source.arrow",
    )

    old_directory = Path.cwd()
    try:
        os.chdir(project)
        destination = ArtifactStore(project / "artifacts").write_failure(
            case,
            pa.table({"id": [1]}),
            _result(),
            runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
            config_sha256="c" * 64,
        )
    finally:
        os.chdir(old_directory)

    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert replay["replay_blockers"] == {"reference": reason}
    assert str(external) not in json.dumps(replay)
    with pytest.raises(ReplayError, match=message) as captured:
        replay_artifact(destination)
    assert "rerun parity check" in str(captured.value)
    assert "configuration directory" in str(captured.value)


def test_artifact_rejects_malformed_config_fingerprint(tmp_path: Path) -> None:
    runtime = collect_runtime_provenance()
    with pytest.raises(ValueError, match="config_sha256"):
        ArtifactStore(tmp_path / "artifacts").write_failure(
            "bad-fingerprint",
            pa.table({"x": [1]}),
            _result(),
            runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
            config_sha256="bad",
        )


def test_artifact_failure_leaves_no_partial_campaign(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("parity.artifacts.pq.write_table", fail)
    root = tmp_path / "artifacts"
    with pytest.raises(OSError, match="disk full"):
        ArtifactStore(root).write_failure("case", pa.table({"x": [1]}), _result())
    assert not list(root.rglob(".pending-*"))


def test_artifact_keeps_lossless_arrow_when_parquet_cannot_represent_schema(
    tmp_path: Path,
) -> None:
    table = pa.table({"metadata": pa.array([{}, None], type=pa.struct([]))})

    destination = ArtifactStore(tmp_path / "artifacts").write_failure(
        "empty-struct", table, _result()
    )

    assert (destination / "input.arrow").is_file()
    assert not (destination / "input.parquet").exists()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert "input.arrow" in manifest["files"]
    assert "input.parquet" not in manifest["files"]


def test_artifact_persists_named_input_bundle_atomically(tmp_path: Path) -> None:
    destination = ArtifactStore(
        tmp_path / "artifacts", invocation_directory=tmp_path
    ).write_failure(
        "orders-join",
        {
            "orders": pa.table({"customer_id": [1, 2], "amount": [10, 20]}),
            "customers": pa.table({"id": [1, 2], "name": ["A", "B"]}),
        },
        _result(),
        source="generated:shrunk",
    )

    names = {path.name for path in destination.iterdir()}
    assert names == {
        "input-000.arrow",
        "input-000.parquet",
        "input-001.arrow",
        "input-001.parquet",
        "manifest.json",
        "replay.json",
        "result.json",
    }
    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert replay["version"] == 2
    assert replay["path_base"] == {"kind": "artifact_ancestor", "levels": 3}
    assert replay["inputs"] == [
        {"name": "orders", "file": "input-000.arrow"},
        {"name": "customers", "file": "input-001.arrow"},
    ]
    assert replay["case"]["input_bundle"]["inputs"] == {
        "orders": {"fixture": "input-000.arrow"},
        "customers": {"fixture": "input-001.arrow"},
    }
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert set(manifest["files"]) == names - {"manifest.json"}
    for name, metadata in manifest["files"].items():
        content = (destination / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == metadata["sha256"]


def test_artifact_rejects_empty_or_invalid_input_bundles(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="two or three"):
        store.write_failure("empty", {}, _result())
    with pytest.raises(TypeError, match=r"pyarrow\.Table"):
        store.write_failure("invalid", {"orders": object()}, _result())  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="two or three"):
        store.write_failure("one", {"orders": pa.table({"x": [1]})}, _result())
    with pytest.raises(ValueError, match="two or three"):
        store.write_failure(
            "four",
            {name: pa.table({"x": [1]}) for name in ("one", "two", "three", "four")},
            _result(),
        )
    with pytest.raises(TypeError, match="Arrow table or a map"):
        store.write_failure(  # type: ignore[arg-type]
            "sequence",
            [pa.table({"x": [1]}), pa.table({"x": [2]})],
            _result(),
        )


def test_configured_bundle_artifact_requires_exact_names_and_order_without_path_leaks(
    tmp_path: Path,
) -> None:
    private_fixture = tmp_path.parent / "private-third.arrow"
    case = CaseConfig(
        name="strict-bundle",
        reference=CallableSpec(target="old:transform"),
        candidate=CallableSpec(target="new:transform"),
        input_bundle=InputBundle(
            inputs={
                "zebra": InputSpec(fixture=tmp_path / "zebra.arrow"),
                "alpha": InputSpec(fixture=tmp_path / "alpha.arrow"),
                "third": InputSpec(fixture=private_fixture),
            }
        ),
    )
    store = ArtifactStore(tmp_path / "artifacts")
    tables = {
        "zebra": pa.table({"x": [1]}),
        "alpha": pa.table({"x": [2]}),
    }

    with pytest.raises(ValueError, match="names and order"):
        store.write_failure(case, tables, _result())
    with pytest.raises(ValueError, match="names and order"):
        store.write_failure(
            case,
            {
                "alpha": tables["alpha"],
                "zebra": tables["zebra"],
                "third": pa.table({"x": [3]}),
            },
            _result(),
        )

    persisted_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "artifacts").rglob("*.json")
    )
    assert str(private_fixture) not in persisted_text
