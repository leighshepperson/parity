from __future__ import annotations

from pathlib import Path

from parity.engine import replay_artifact, run_suite
from parity.models import (
    CallableSpec,
    CaseConfig,
    ColumnSchema,
    FrameSchema,
    GenerationConfig,
    MismatchKind,
    ParityConfig,
    PerformanceConfig,
    Status,
)


def test_same_class_field_regressions_are_discovered_shrunk_and_replayed_independently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "independent_regressions.py").write_text(
        "def reference(frame):\n"
        "    return frame.copy()\n"
        "def candidate(frame):\n"
        "    result = frame.copy()\n"
        "    if result.iloc[0]['selector'] == 0:\n"
        "        result['left'] += 1\n"
        "    else:\n"
        "        result['right'] += 1\n"
        "    return result\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="independent-value-regressions",
        reference=CallableSpec(
            target="independent_regressions:reference",
            adapter="pandas",
            workdir=tmp_path,
        ),
        candidate=CallableSpec(
            target="independent_regressions:candidate",
            adapter="pandas",
            workdir=tmp_path,
        ),
        input_schema=FrameSchema(
            columns=[
                ColumnSchema(
                    name="selector",
                    dtype="integer",
                    nullable=False,
                    minimum=0,
                    maximum=1,
                ),
                ColumnSchema(
                    name="left",
                    dtype="integer",
                    nullable=False,
                    minimum=0,
                    maximum=5,
                ),
                ColumnSchema(
                    name="right",
                    dtype="integer",
                    nullable=False,
                    minimum=0,
                    maximum=5,
                ),
            ],
            min_rows=1,
            max_rows=3,
        ),
        generation=GenerationConfig(
            max_examples=200,
            max_findings=2,
            stability_repeats=1,
            seed=20260820,
            adversarial_examples=False,
        ),
        performance=PerformanceConfig(enabled=False),
    )

    result = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    observed = result.cases[0]
    assert result.status is observed.status is Status.FAILED
    assert observed.findings_discovered == 2
    assert observed.generated_examples > 0
    signatures = {failure.finding_signature for failure in observed.failures}
    assert None not in signatures
    assert len(signatures) == 2
    assert all(failure.source == "generated:shrunk" for failure in observed.failures)
    assert {
        mismatch.path.rsplit(".", 1)[-1]
        for failure in observed.failures
        for mismatch in failure.mismatches
        if mismatch.kind is MismatchKind.VALUE and mismatch.path is not None
    } == {"left", "right"}

    for failure in observed.failures:
        assert failure.artifact is not None
        replayed = replay_artifact(failure.artifact)
        assert replayed.status is Status.FAILED
        assert replayed.cases[0].failures[0].finding_signature == failure.finding_signature
