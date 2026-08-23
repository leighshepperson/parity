from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest

from parity.engine import replay_artifact, run_suite
from parity.invocation import FrameSequence, Invocation, resolve_invocation
from parity.models import (
    CallableSpec,
    CaseConfig,
    ColumnSchema,
    ComparisonPolicy,
    EqualRowCount,
    FrameArgument,
    FrameSchema,
    FrameSequenceArgument,
    GenerationConfig,
    InvocationConfig,
    JsonArgument,
    ParityConfig,
    PerformanceConfig,
    Status,
)


def _write_arrow(path: Path, values: list[int]) -> None:
    table = pa.table({"key": values, "value": [value * 10 for value in values]})
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)


def _schema() -> FrameSchema:
    return FrameSchema(
        columns=[
            ColumnSchema(
                name="key",
                dtype="integer",
                nullable=False,
                minimum=0,
                maximum=4,
            ),
            ColumnSchema(
                name="value",
                dtype="integer",
                nullable=False,
                minimum=-20,
                maximum=40,
            ),
        ],
        min_rows=0,
        max_rows=3,
    )


def test_runtime_invocation_enforces_protocol_safety_bounds() -> None:
    table = pa.table({"value": [1]})

    with pytest.raises(ValueError, match="more than 256 positional"):
        Invocation(tuple(None for _ in range(257)))
    with pytest.raises(ValueError, match="more than 256 items"):
        FrameSequence(tuple(table for _ in range(257)))
    with pytest.raises(ValueError, match="256 KiB"):
        Invocation(args=("x" * (256 * 1024),))
    with pytest.raises(ValueError, match="512 KiB in total"):
        Invocation(args=("x" * 200_000, "y" * 200_000, "z" * 200_000))


def test_dataframe_shaped_json_remains_json_when_declared_as_json() -> None:
    value = {"rows": [{"key": 1, "value": 2}], "columns": ["key", "value"]}

    direct = Invocation(args=(value,))
    resolved = resolve_invocation(
        InvocationConfig(args=[JsonArgument(values=[value])]),
        adversarial=False,
        search=False,
    )

    assert direct.args == (value,)
    assert resolved.deterministic[0][1].args == (value,)


def test_fixed_empty_frame_sequence_needs_no_item_schema() -> None:
    resolved = resolve_invocation(
        InvocationConfig(
            args=[
                FrameSequenceArgument(
                    generate=False,
                    min_items=0,
                    max_items=0,
                    container="list",
                )
            ]
        ),
        adversarial=True,
        search=True,
    )

    invocation = resolved.deterministic[0][1]
    assert invocation.args == (FrameSequence((), "list"),)
    assert resolved.strategy is not None


def _write_targets(path: Path) -> None:
    path.write_text(
        """
import pandas as pd
import polars as pl


def constant_reference():
    return {"ready": True, "value": 42}


def constant_candidate():
    return {"ready": True, "value": 42}


def pandas_reduce(operation, *frames, batches, descending):
    assert operation == "sum"
    combined = pd.concat([*frames, *batches], ignore_index=True)
    result = combined.groupby("key", as_index=False, dropna=False)["value"].sum()
    return result.sort_values("key", ascending=not descending, ignore_index=True)


def polars_reduce(operation, *frames, batches, descending):
    assert operation == "sum"
    combined = pl.concat([*frames, *batches], how="vertical")
    return (
        combined.group_by("key")
        .agg(pl.col("value").sum())
        .sort("key", descending=descending)
    )


def polars_defect(operation, *frames, batches, descending):
    result = polars_reduce(
        operation, *frames, batches=batches, descending=descending
    )
    if descending and result.height:
        return result.with_columns((pl.col("value") + 1).alias("value"))
    return result
""",
        encoding="utf-8",
    )


def _variable_contract() -> InvocationConfig:
    schema = _schema()
    return InvocationConfig(
        args=[JsonArgument(values=["sum"])],
        kwargs={
            "batches": FrameSequenceArgument(
                name="batches",
                input_schema=schema,
                min_items=1,
                max_items=3,
                container="list",
            ),
            "descending": JsonArgument(values=[False, True]),
        },
        varargs=FrameSequenceArgument(
            name="frames",
            input_schema=schema,
            min_items=4,
            max_items=6,
            container="tuple",
        ),
    )


def _case(tmp_path: Path, candidate: str, *, max_examples: int) -> CaseConfig:
    return CaseConfig(
        name="variable-reduce",
        reference=CallableSpec(
            target="complex_targets:pandas_reduce", adapter="pandas", workdir=tmp_path
        ),
        candidate=CallableSpec(
            target=f"complex_targets:{candidate}", adapter="polars", workdir=tmp_path
        ),
        invocation=_variable_contract(),
        comparison=ComparisonPolicy(row_order="keyed", row_keys=["key"]),
        generation=GenerationConfig(
            max_examples=max_examples,
            max_findings=1,
            adversarial_examples=False,
            stability_repeats=1,
            derandomize=True,
        ),
        performance=PerformanceConfig(enabled=False),
    )


def test_zero_argument_case_executes_in_isolated_workers(tmp_path: Path, monkeypatch) -> None:
    _write_targets(tmp_path / "complex_targets.py")
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="constant",
        reference=CallableSpec(target="complex_targets:constant_reference", workdir=tmp_path),
        candidate=CallableSpec(target="complex_targets:constant_candidate", workdir=tmp_path),
        invocation=InvocationConfig(),
        generation=GenerationConfig(search=False, stability_repeats=1),
        performance=PerformanceConfig(enabled=False),
    )

    result = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    assert result.status is Status.PASSED
    assert result.cases[0].examples_run == 1


def test_variable_arity_reduce_matches_across_pandas_and_polars(
    tmp_path: Path, monkeypatch
) -> None:
    _write_targets(tmp_path / "complex_targets.py")
    monkeypatch.chdir(tmp_path)

    result = run_suite(
        ParityConfig(
            artifact_dir=tmp_path / ".parity",
            cases=[_case(tmp_path, "polars_reduce", max_examples=25)],
        )
    )

    assert result.status is Status.PASSED
    assert result.cases[0].generated_examples >= 25


def test_recursive_variable_arity_failure_replays_exactly(tmp_path: Path, monkeypatch) -> None:
    _write_targets(tmp_path / "complex_targets.py")
    monkeypatch.chdir(tmp_path)
    result = run_suite(
        ParityConfig(
            artifact_dir=tmp_path / ".parity",
            cases=[_case(tmp_path, "polars_defect", max_examples=25)],
        )
    )

    failure = result.cases[0].failures[0]
    assert result.status is Status.FAILED
    assert failure.artifact is not None
    replay = json.loads((failure.artifact / "replay.json").read_text(encoding="utf-8"))
    assert replay["version"] == 3
    assert len(replay["invocation"]["args"]) >= 5
    assert replay["invocation"]["args"][0] == {"kind": "json", "value": "sum"}
    assert replay["invocation"]["kwargs"]["batches"]["kind"] == "frames"
    assert replay["invocation"]["kwargs"]["descending"] == {
        "kind": "json",
        "value": True,
    }

    replayed = replay_artifact(failure.artifact)

    assert replayed.status is Status.FAILED
    assert replayed.cases[0].failures[0].finding_signature == failure.finding_signature


def test_relationships_preserve_fixture_baseline_and_leave_unrelated_frames_independent(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left.arrow"
    right = tmp_path / "right.arrow"
    extra = tmp_path / "extra.arrow"
    _write_arrow(left, [1, 2])
    _write_arrow(right, [2, 3])
    _write_arrow(extra, [4])
    contract = InvocationConfig(
        args=[
            FrameArgument(name="left", fixture=left, input_schema=_schema()),
            FrameArgument(name="right", fixture=right, input_schema=_schema()),
            FrameArgument(name="extra", fixture=extra, input_schema=_schema()),
        ],
        relationships=[EqualRowCount(inputs=["left", "right"])],
    )

    resolved = resolve_invocation(contract, adversarial=True, search=False)
    baseline = resolved.deterministic[0][1]

    assert isinstance(baseline, Invocation)
    assert baseline.args[0].column("key").to_pylist() == [1, 2]
    assert baseline.args[1].column("key").to_pylist() == [2, 3]
    assert baseline.args[2].column("key").to_pylist() == [4]
    assert any(source.startswith("invocation:args/2:") for source, _ in resolved.deterministic)


def test_fixed_sequence_and_varargs_preserve_their_distinct_call_shapes(tmp_path: Path) -> None:
    paths = [tmp_path / f"frame-{index}.arrow" for index in range(7)]
    for index, path in enumerate(paths):
        _write_arrow(path, [index % 5])
    contract = InvocationConfig(
        args=[
            *[
                FrameArgument(name=f"fixed_{index}", fixture=path, generate=False)
                for index, path in enumerate(paths[:5])
            ],
            JsonArgument(values=["sum"]),
        ],
        kwargs={
            "batches": FrameSequenceArgument(
                fixtures=paths[5:],
                min_items=2,
                max_items=2,
                container="list",
                generate=False,
            )
        },
        varargs=FrameSequenceArgument(
            fixtures=paths[5:],
            min_items=2,
            max_items=2,
            container="tuple",
            generate=False,
        ),
    )

    invocation = resolve_invocation(contract, adversarial=False, search=False).deterministic[0][1]

    assert len(invocation.args) == 8
    assert invocation.args[5] == "sum"
    assert all(isinstance(value, pa.Table) for value in invocation.args[6:])
    assert isinstance(invocation.kwargs["batches"], FrameSequence)
    assert invocation.kwargs["batches"].container == "list"
