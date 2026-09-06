from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from parity import compare
from parity.cli import app
from parity.corpus import CorpusError
from parity.engine import replay_artifact
from parity.models import CallableSpec, ComparisonPolicy, Status
from parity.pytest_plugin import ParityAssertions

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "target.py").write_text(
        """
counter = 0

def old(items=(), count=0, *, region="GB"):
    if len(items) != count:
        raise ValueError("invalid correlated call")
    return {"sum": sum(items), "region": region}

def new(*args, **kwargs):
    value = old(*args, **kwargs)
    if value["region"] == "US":
        value["sum"] += 1
    return value

def crash(*args, **kwargs):
    import os
    os._exit(4)

def changing(*args, **kwargs):
    global counter
    counter += 1
    return counter

def hangs(*args, **kwargs):
    import time
    time.sleep(30)

def mutate(items, count, **kwargs):
    value = old(items, count, **kwargs)
    items.append(99)
    return value

def raises(*args, **kwargs):
    raise ValueError("invalid request")

def exact():
    return 1000000000.0

def close():
    return 1000000001.0
""",
        encoding="utf-8",
    )
    (tmp_path / "calls.jsonl").write_text(
        "\n".join(
            json.dumps(call)
            for call in [
                {"args": [[1, 2], 2], "kwargs": {"region": "GB"}},
                {"args": [[9], 1], "kwargs": {"region": "US"}},
                {},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_compare_checks_exact_calls_and_replays_without_corpus(project: Path) -> None:
    result = compare("target:old", "target:new", calls="calls.jsonl")
    assert result.status is Status.FAILED
    case = result.cases[0]
    assert case.examples_run == 3
    assert case.generated_examples == 0
    assert case.performance is None
    assert len(case.failures) == 1
    failure = case.failures[0]
    assert failure.source == "calls:line:2"
    assert failure.artifact is not None
    (project / "calls.jsonl").unlink()
    replay = replay_artifact(failure.artifact)
    assert replay.status is Status.FAILED
    assert replay.cases[0].failures[0].finding_signature == failure.finding_signature


def test_compare_passes_preserves_corpus_and_binds_evidence_to_content(project: Path) -> None:
    original = (project / "calls.jsonl").read_bytes()
    spec = CallableSpec(target="target:old")
    result = compare(spec, spec, calls="calls.jsonl")
    assert result.passed
    assert result.cases[0].examples_run == 3
    assert spec.workdir is None
    assert (project / "calls.jsonl").read_bytes() == original
    (project / "calls.jsonl").write_text("{}\n", encoding="utf-8")
    changed = compare(spec, spec, calls="calls.jsonl")
    assert changed.passed
    assert changed.provenance.config_sha256 != result.provenance.config_sha256


def test_invalid_later_call_prevents_any_target_execution(project: Path) -> None:
    (project / "target.py").write_text(
        'from pathlib import Path\nPath("IMPORTED").touch()\ndef old(): return 1\n',
        encoding="utf-8",
    )
    (project / "calls.jsonl").write_text('{}\n{"secret":"PRIVATE"}\n', encoding="utf-8")
    with pytest.raises(CorpusError, match="line 2"):
        compare("target:old", "target:old", calls="calls.jsonl")
    assert not (project / "IMPORTED").exists()


@pytest.mark.parametrize("target", ["target:crash", "target:changing", "target:missing"])
def test_unreliable_targets_are_errors(project: Path, target: str) -> None:
    result = compare(target, target, calls="calls.jsonl")
    assert result.status is Status.ERROR


def test_target_timeout_is_an_error(project: Path) -> None:
    result = compare("target:hangs", "target:hangs", calls="calls.jsonl", timeout_seconds=2)
    assert result.status is Status.ERROR


def test_matching_exceptions_pass_without_a_benchmark(project: Path) -> None:
    result = compare("target:raises", "target:raises", calls="calls.jsonl")
    assert result.passed


def test_input_mutation_is_detected(project: Path) -> None:
    result = compare("target:old", "target:mutate", calls="calls.jsonl", max_findings=1)
    assert result.status is Status.FAILED
    assert any(m.kind.value == "mutation" for m in result.cases[0].failures[0].mismatches)


def test_numbers_are_exact_unless_tolerance_is_requested(project: Path) -> None:
    (project / "calls.jsonl").write_text("{}\n", encoding="utf-8")
    strict = compare("target:exact", "target:close", calls="calls.jsonl")
    assert strict.status is Status.FAILED
    relaxed = compare(
        "target:exact", "target:close", calls="calls.jsonl", comparison=ComparisonPolicy(rtol=1e-7)
    )
    assert relaxed.passed


def test_separate_checkouts_can_expose_the_same_import_name(project: Path) -> None:
    before, after = project / "before", project / "after"
    before.mkdir()
    after.mkdir()
    (before / "business.py").write_text('def run(*a, **kw): return "old"\n', encoding="utf-8")
    (after / "business.py").write_text('def run(*a, **kw): return "new"\n', encoding="utf-8")
    outcome = runner.invoke(
        app,
        [
            "compare",
            "business:run",
            "business:run",
            "--calls",
            "calls.jsonl",
            "--reference-workdir",
            str(before),
            "--candidate-workdir",
            str(after),
            "--json",
            "report.json",
            "--junit",
            "junit.xml",
        ],
    )
    assert outcome.exit_code == 1, outcome.output
    assert "calls line 1" in outcome.output
    assert json.loads((project / "report.json").read_text())["status"] == "failed"
    assert (project / "junit.xml").exists()
    assert "no generated inputs" in outcome.output


def test_cli_exit_codes_and_safe_corpus_error(project: Path) -> None:
    passing = runner.invoke(app, ["compare", "target:old", "target:old", "--calls", "calls.jsonl"])
    assert passing.exit_code == 0, passing.output
    missing = runner.invoke(
        app, ["compare", "target:missing", "target:old", "--calls", "calls.jsonl"]
    )
    assert missing.exit_code == 2, missing.output
    (project / "calls.jsonl").write_text('{"args":["PRIVATE"],"oops":1}\n', encoding="utf-8")
    invalid = runner.invoke(app, ["compare", "target:old", "target:old", "--calls", "calls.jsonl"])
    assert invalid.exit_code == 2
    assert "line 1" in invalid.output
    assert "PRIVATE" not in invalid.output


def test_pytest_facade_reports_the_differing_call(project: Path) -> None:
    assertions = ParityAssertions("unused.toml")
    assert assertions.compare("target:old", "target:old", calls="calls.jsonl").passed
    with pytest.raises(pytest.fail.Exception, match="semantic parity check failed") as caught:
        assertions.compare("target:old", "target:new", calls="calls.jsonl")
    assert "replay artifact" in str(caught.value)


def test_dataframe_returns_from_json_requests_are_compared(project: Path) -> None:
    (project / "frames.py").write_text(
        """
def before(records):
    import pandas as pd
    return pd.DataFrame(records).groupby("group", as_index=False, sort=True)["amount"].sum()

def after(records):
    import polars as pl
    return pl.DataFrame(records).group_by("group").agg(pl.col("amount").sum()).sort("group")

def fixed(records):
    import polars as pl
    return pl.DataFrame(records).drop_nulls("group").group_by("group").agg(pl.col("amount").sum()).sort("group")
""",
        encoding="utf-8",
    )
    calls = [
        {"args": [[{"group": "a", "amount": 2}, {"group": "a", "amount": 3}]]},
        {"args": [[{"group": "a", "amount": 2}, {"group": None, "amount": 3}]]},
    ]
    (project / "calls.jsonl").write_text(
        "".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8"
    )
    changed = compare("frames:before", "frames:after", calls="calls.jsonl")
    assert changed.status is Status.FAILED
    assert changed.cases[0].failures[0].source == "calls:line:2"
    fixed = compare("frames:before", "frames:fixed", calls="calls.jsonl")
    assert fixed.passed
