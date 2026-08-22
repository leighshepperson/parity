"""Execute and assert the bounded or extended order-book migration proof."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
from candidate import correct_port, naive_port


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
        rendered = shlex.join(command)
        raise ProofError(
            f"{rendered} exited {completed.returncode}, expected {expected}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _report(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ProofError(f"{path.name} was not a JSON object")
    return parsed


def _capture(
    target: Callable[[pa.Table, pa.Table], pa.Table], bundle: Mapping[str, pa.Table]
) -> tuple[str, object]:
    try:
        return "returned", target(bundle["events"], bundle["instruments"]).to_pylist()
    except Exception as error:  # the exception itself is part of this contract
        return "raised", (type(error).__module__, type(error).__qualname__, str(error))


def _assert_witnesses(proof: Path, report: Mapping[str, Any]) -> list[tuple[Path, str]]:
    case = report["cases"][0]
    failures = case["failures"]
    if report.get("status") != "failed" or case.get("findings_discovered") != 5:
        raise ProofError("naive Python port did not produce exactly five semantic findings")
    if any(failure.get("source") != "generated:custom:shrunk" for failure in failures):
        raise ProofError("a naive-port finding was not generated and shrunk")

    witnesses: list[tuple[Path, str]] = []
    row_counts: list[int] = []
    signatures: set[str] = set()
    for failure in failures:
        artifact = proof / failure["artifact"]
        with (artifact / "input-000.arrow").open("rb") as stream:
            events = ipc.open_file(stream).read_all()
        with (artifact / "input-001.arrow").open("rb") as stream:
            instruments = ipc.open_file(stream).read_all()
        bundle = {"events": events, "instruments": instruments}
        if _capture(correct_port, bundle) == _capture(naive_port, bundle):
            raise ProofError("a retained witness does not distinguish the two Python ports")
        signature = failure.get("finding_signature")
        if not isinstance(signature, str):
            raise ProofError("a retained finding has no signature")
        signatures.add(signature)
        row_counts.append(events.num_rows)
        witnesses.append((artifact, signature))

    if len(signatures) != 5 or sorted(row_counts) != [2, 3, 7, 7, 10]:
        raise ProofError(
            f"unexpected minimized witnesses: signatures={len(signatures)}, rows={row_counts}"
        )
    return witnesses


def _copy_proof(source: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(".parity*", "__pycache__", "reports")
    shutil.copytree(source, destination, ignore=ignored)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    parity = shutil.which("parity")
    if parity is None:
        raise ProofError("Parity executable is missing")

    source = Path(__file__).resolve().parent
    binary = source / "bin" / "legacy_orderbook"
    if not binary.is_file():
        raise ProofError("compile bin/legacy_orderbook before running the proof")

    correct_examples = 120 if arguments.profile == "quick" else 750
    naive_examples = 100 if arguments.profile == "quick" else 500
    naive_findings = 5 if arguments.profile == "quick" else 6

    with tempfile.TemporaryDirectory(prefix="parity-cpp-python-") as temporary:
        root = Path(temporary)
        proof = root / "proof"
        _copy_proof(source, proof)
        reports = proof / "reports"
        reports.mkdir()

        _run([parity, "doctor", "--config", "parity.toml", "--json"], cwd=proof, expected=0)

        correct_path = reports / "correct.json"
        correct_command = [
            parity,
            "check",
            "--config",
            "parity.toml",
            "--case",
            "correct-port",
            "--max-examples",
            str(correct_examples),
            "--json",
            str(correct_path),
        ]
        if arguments.profile == "quick":
            correct_command.append("--no-performance")
        _run(correct_command, cwd=proof, expected=0)
        correct = _report(correct_path)
        correct_case = correct["cases"][0]
        if (
            correct.get("status") != "passed"
            or correct_case.get("examples_run") != correct_examples
            or correct_case.get("findings_discovered") != 0
        ):
            raise ProofError("correct Python port did not pass the generated campaign")
        if arguments.profile == "full" and correct_case.get("performance") is None:
            raise ProofError("full profile did not retain performance evidence")
        print(f"PASS correct port agrees with C++ ({correct_examples} generated streams)")

        defect_path = reports / "defect.json"
        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--case",
                "naive-port",
                "--max-examples",
                str(naive_examples),
                "--max-findings",
                str(naive_findings),
                "--no-performance",
                "--json",
                str(defect_path),
            ],
            cwd=proof,
            expected=1,
        )
        witnesses = _assert_witnesses(proof, _report(defect_path))
        print("PASS naive port produced five distinct minimized findings")

        regression_path = reports / "regressions.json"
        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--tag",
                "regression",
                "--no-performance",
                "--json",
                str(regression_path),
            ],
            cwd=proof,
            expected=0,
        )
        regressions = _report(regression_path)
        if regressions.get("status") != "passed" or len(regressions.get("cases", [])) != 5:
            raise ProofError("correct Python port did not pass all five retained regressions")
        print("PASS correct port passes all five retained regressions")

        replay_directory = root / "unrelated-replay-directory"
        replay_directory.mkdir()
        for artifact, signature in witnesses:
            replay = _run(
                [parity, "replay", str(artifact), "--json"],
                cwd=replay_directory,
                expected=1,
            )
            document = json.loads(replay.stdout)
            replayed = document["result"]["cases"][0]["failures"][0]
            if document.get("status") != "failed" or replayed.get("finding_signature") != signature:
                raise ProofError("replay did not reproduce a retained finding signature")
        print("PASS all five findings replay from an unrelated working directory")


if __name__ == "__main__":
    try:
        main()
    except ProofError as error:
        raise SystemExit(f"FAIL {error}") from None
