"""Execute and assert the complete JavaScript-to-Python rules migration proof."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from candidate import correct_port, naive_port

from parity import Invocation


class ProofError(RuntimeError):
    """The case study did not establish one of its promised properties."""


def _run(command: list[str], *, cwd: Path, expected: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != expected:
        raise ProofError(
            f"{shlex.join(command)} exited {completed.returncode}, expected {expected}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _report(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ProofError(f"{path.name} was not a JSON object")
    return parsed


def _capture(target: Callable[..., object], invocation: Invocation) -> tuple[str, object]:
    try:
        return "returned", target(*invocation.args, **invocation.kwargs)
    except Exception as error:
        return "raised", (type(error).__module__, type(error).__qualname__, str(error))


def _saved_invocation(artifact: Path) -> Invocation:
    replay = _report(artifact / "replay.json")
    document = replay.get("invocation")
    if not isinstance(document, dict):
        raise ProofError("artifact has no invocation document")
    args = document.get("args")
    kwargs = document.get("kwargs")
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ProofError("artifact invocation has an invalid call shape")
    nodes = [*args, *kwargs.values()]
    if any(not isinstance(node, dict) or set(node) != {"kind", "value"} for node in nodes):
        raise ProofError("rules artifact was not stored as JSON-only input")
    if any(node["kind"] != "json" for node in nodes):
        raise ProofError("rules artifact unexpectedly contains a tabular input")
    if list(artifact.glob("*.arrow")):
        raise ProofError("rules artifact unexpectedly contains Arrow data")
    return Invocation(
        args=tuple(node["value"] for node in args),
        kwargs={name: node["value"] for name, node in kwargs.items()},
    )


def _defect_family(invocation: Invocation) -> str:
    correct = _capture(correct_port, invocation)
    naive = _capture(naive_port, invocation)
    if correct == naive:
        raise ProofError("saved invocation does not distinguish the Python ports")
    if correct[0] == "returned" and naive[0] == "raised":
        return "eager-evaluation"
    if correct[0] != "returned" or naive[0] != "returned":
        raise ProofError("saved invocation has an unexpected outcome pair")
    expected = correct[1]
    actual = naive[1]
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        raise ProofError("saved invocation did not return rules result objects")
    if expected.get("total") != actual.get("total") or len(expected.get("trace", [])) != len(
        actual.get("trace", [])
    ):
        return "first-match"
    if expected.get("decision") != actual.get("decision"):
        return "exclusive-threshold"
    raise ProofError("saved invocation did not map to a known injected defect")


def _copy_proof(source: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(".parity*", "__pycache__", "reports")
    shutil.copytree(source, destination, ignore=ignored)


def main(profile: str) -> None:
    parity = shutil.which("parity")
    node = shutil.which("node")
    if parity is None:
        raise ProofError("Parity executable is missing")
    if node is None:
        raise ProofError("Node.js executable is missing")
    limits = {
        "quick": {"correct": 60, "naive": 80},
        "full": {"correct": 250, "naive": 250},
    }[profile]

    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="parity-javascript-rules-", dir="/tmp") as temporary:
        root = Path(temporary)
        proof = root / "proof"
        _copy_proof(source, proof)

        _run([parity, "doctor", "--config", "parity.toml", "--json"], cwd=proof, expected=0)

        correct_report_path = proof / "reports" / "correct.json"
        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--case",
                "correct-port",
                "--max-examples",
                str(limits["correct"]),
                "--no-performance",
                "--json",
                str(correct_report_path),
            ],
            cwd=proof,
            expected=0,
        )
        correct_report = _report(correct_report_path)
        correct_case = correct_report["cases"][0]
        if correct_report.get("status") != "passed" or correct_case["generated_examples"] < 1:
            raise ProofError("correct Python port did not pass generated JavaScript controls")
        print(
            "PASS correct Python port agrees with JavaScript "
            f"({correct_case['generated_examples']} generated programs)"
        )

        naive_report_path = proof / "reports" / "naive.json"
        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--case",
                "naive-port",
                "--max-examples",
                str(limits["naive"]),
                "--max-findings",
                "4",
                "--no-performance",
                "--json",
                str(naive_report_path),
            ],
            cwd=proof,
            expected=1,
        )
        naive_report = _report(naive_report_path)
        naive_case = naive_report["cases"][0]
        failures = naive_case["failures"]
        if naive_report.get("status") != "failed" or naive_case["findings_discovered"] != 3:
            raise ProofError("naive Python port did not produce exactly three findings")
        if any(failure.get("source") != "generated:custom:shrunk" for failure in failures):
            raise ProofError("not every rules finding was generated and shrunk")

        artifacts = [proof / failure["artifact"] for failure in failures]
        families = {_defect_family(_saved_invocation(artifact)) for artifact in artifacts}
        expected_families = {"eager-evaluation", "first-match", "exclusive-threshold"}
        if families != expected_families:
            raise ProofError(f"unexpected discovered defect families: {sorted(families)}")
        print("PASS naive Python port produced three distinct minimized findings")
        print("PASS every minimized invocation is recursive JSON with no tabular inputs")

        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--tag",
                "regression",
                "--max-examples",
                "1",
                "--no-performance",
            ],
            cwd=proof,
            expected=0,
        )
        print("PASS correct port passes all three retained regressions")

        replay_directory = root / "unrelated-replay-directory"
        replay_directory.mkdir()
        for artifact, failure in zip(artifacts, failures, strict=True):
            replay = _run(
                [parity, "replay", str(artifact), "--json"],
                cwd=replay_directory,
                expected=1,
            )
            replay_document = json.loads(replay.stdout)
            replay_failure = replay_document["result"]["cases"][0]["failures"][0]
            if replay_failure.get("finding_signature") != failure.get("finding_signature"):
                raise ProofError("replay did not reproduce the original finding signature")
        print("PASS all three findings replay from an unrelated working directory")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    arguments = parser.parse_args()
    try:
        main(arguments.profile)
    except ProofError as error:
        raise SystemExit(f"FAIL {error}") from None
