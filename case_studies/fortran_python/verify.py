"""Execute and assert the complete single-container migration proof."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
from candidate import correct_port, naive_port


class ProofError(RuntimeError):
    """The container did not establish one of the promised properties."""


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


def _assert_minimal_witness(artifact: Path) -> None:
    with (artifact / "input.arrow").open("rb") as stream:
        witness = ipc.open_file(stream).read_all()
    if witness.num_rows != 3:
        raise ProofError(f"expected a three-row witness, received {witness.num_rows} rows")
    if correct_port(witness) == naive_port(witness):
        raise ProofError("retained witness does not distinguish the two Python algorithms")
    values = witness.column("value").to_pylist()
    for removed in range(len(values)):
        reduced = values[:removed] + values[removed + 1 :]
        table = pa.table({"value": pa.array(reduced, type=pa.float64())})
        if correct_port(table) != naive_port(table):
            raise ProofError("retained witness was not deletion-minimal")


def _copy_proof(source: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(".parity*", "__pycache__", "reports")
    shutil.copytree(source, destination, ignore=ignored)


def main() -> None:
    unavailable = ("docker", "gfortran", "tox", "uv")
    present = [tool for tool in unavailable if shutil.which(tool) is not None]
    if present:
        raise ProofError(f"runtime image unexpectedly contains: {', '.join(present)}")
    print("PASS runtime contains no compiler, container CLI, tox or uv")

    parity = shutil.which("parity")
    if parity is None:
        raise ProofError("Parity executable is missing")

    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="parity-fortran-python-", dir="/tmp") as temporary:
        root = Path(temporary)
        proof = root / "proof"
        _copy_proof(source, proof)

        _run(
            [parity, "doctor", "--config", "parity.toml", "--json"],
            cwd=proof,
            expected=0,
        )

        correct_report_path = proof / "reports" / "correct.json"
        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--case",
                "correct-port",
                "--no-performance",
                "--json",
                str(correct_report_path),
            ],
            cwd=proof,
            expected=0,
        )
        correct_report = _report(correct_report_path)
        if correct_report.get("status") != "passed":
            raise ProofError("correct Python port did not pass")
        correct_examples = correct_report["cases"][0]["examples_run"]
        print(f"PASS correct port agrees with Fortran ({correct_examples} observations)")

        defect_report_path = proof / "reports" / "defect.json"
        _run(
            [
                parity,
                "check",
                "--config",
                "parity.toml",
                "--case",
                "naive-port-cancellation",
                "--no-performance",
                "--json",
                str(defect_report_path),
            ],
            cwd=proof,
            expected=1,
        )
        defect_report = _report(defect_report_path)
        defect_case = defect_report["cases"][0]
        if defect_report.get("status") != "failed" or defect_case["findings_discovered"] != 1:
            raise ProofError("defective Python port did not produce exactly one semantic finding")
        failure = defect_case["failures"][0]
        if failure.get("source") != "generated:shrunk":
            raise ProofError("defective port finding was not generated and shrunk")
        artifact = proof / failure["artifact"]
        _assert_minimal_witness(artifact)
        print("PASS naive port is rejected with a three-row counterexample")

        replay_directory = root / "unrelated-replay-directory"
        replay_directory.mkdir()
        replay = _run(
            [parity, "replay", str(artifact), "--json"],
            cwd=replay_directory,
            expected=1,
        )
        replay_document = json.loads(replay.stdout)
        replay_failure = replay_document["result"]["cases"][0]["failures"][0]
        if replay_document.get("status") != "failed" or (
            replay_failure.get("finding_signature") != failure.get("finding_signature")
        ):
            raise ProofError("replay did not reproduce the original finding signature")
        print("PASS replay reproduces the same finding from another working directory")


if __name__ == "__main__":
    try:
        main()
    except ProofError as error:
        raise SystemExit(f"FAIL {error}") from None
