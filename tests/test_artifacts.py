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
    ExampleResult,
    Mismatch,
    MismatchKind,
    Status,
)


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
