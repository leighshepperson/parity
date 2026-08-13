from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pytest

from parity.artifacts import ArtifactStore
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseProvenance,
    ExampleResult,
    InputBundle,
    InputSpec,
    Mismatch,
    MismatchKind,
    Status,
)
from parity.provenance import collect_runtime_provenance


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


def test_artifact_campaign_is_complete_replayable_and_hashed(tmp_path: Path) -> None:
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
    assert replay["version"] == 1
    assert "expected_runtime" not in replay
    assert replay["command"] == ["parity", "replay", "<artifact-path>"]
    assert replay["working_directory"] == "original invocation directory"
    assert replay["path_base"] == "invocation_cwd"
    assert replay["case"]["fixture"] == "input.arrow"
    assert replay["case"]["reference"] is None
    assert replay["case"]["static_kwargs"]["api_key"] == "<redacted>"
    assert replay["case"]["static_kwargs"]["mode"] == "strict"
    assert "do-not-store" not in replay_text
    assert "also-secret" not in replay_text
    assert str(tmp_path) not in replay_text


def test_artifact_uses_v2_only_with_complete_runtime_and_config_contract(
    tmp_path: Path,
) -> None:
    runtime = collect_runtime_provenance(["definitely-not-installed-artifact-probe"])
    destination = ArtifactStore(tmp_path / "artifacts").write_failure(
        "complete-runtime",
        pa.table({"x": [1]}),
        _result(),
        runtime_provenance=CaseProvenance(reference=runtime, candidate=runtime),
        config_sha256="a" * 64,
    )

    replay = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    assert replay["version"] == 2
    assert replay["config_sha256"] == "a" * 64
    assert replay["expected_runtime"]["reference"]["python_version"]
    assert replay["expected_runtime"]["candidate"]["distributions"]


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
    destination = ArtifactStore(tmp_path / "artifacts").write_failure(
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
    assert replay["version"] == 3
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
