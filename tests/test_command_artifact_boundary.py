from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pytest

import parity.engine as engine
from parity.config import load_config
from parity.engine import ReplayError, replay_artifact, run_suite
from parity.models import (
    CallableSpec,
    CaseConfig,
    GenerationConfig,
    ParityConfig,
    PerformanceConfig,
    Status,
)
from parity.reporting import render_json, render_markdown, render_terminal


def _write_arrow(path: Path, table: pa.Table) -> None:
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)


def _write_protocol_target(path: Path) -> None:
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import platform
import sys
import time

import pyarrow as pa
import pyarrow.ipc as ipc


def runtime():
    return {{
        "executor": "command",
        "runtime_name": "artifact-boundary-test",
        "runtime_version": "1.0",
        "python_implementation": None,
        "python_version": None,
        "platform_system": platform.system() or "unknown",
        "platform_machine": platform.machine() or "unknown",
        "parity_version": None,
        "distributions": [],
        "identities": [],
    }}


root = os.path.realpath(sys.argv[-1])
for raw_token in sys.stdin.buffer:
    started = time.perf_counter()
    token = raw_token.rstrip(b"\\r\\n").decode("ascii")
    call_root = os.path.join(root, token)
    with open(os.path.join(call_root, "request.json"), encoding="utf-8") as stream:
        request = json.load(stream)
    response = {{
        "protocol_version": 1,
        "duration_seconds": 0.0,
        "mutated_inputs": [],
        "return_type": None,
        "runtime": runtime(),
        "output": None,
        "exception": None,
        "outcome": "returned",
    }}
    if request["operation"] == "execute":
        source = request["inputs"]["items"][0]["path"]
        with open(source, "rb") as stream:
            table = ipc.open_file(stream).read_all()
        output = pa.table({{"value": table.column("value")}})
        with open(request["output"]["arrow"], "wb") as stream:
            with ipc.new_file(stream, output.schema) as writer:
                writer.write_table(output)
        response["return_type"] = "test.command.Output"
        response["output"] = {{"kind": "arrow"}}
    response["duration_seconds"] = time.perf_counter() - started
    destination = os.path.join(call_root, "response.json")
    temporary = destination + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(response, stream, sort_keys=True)
    os.replace(temporary, destination)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_toml_accepts_command_and_python_canonicalizer_endpoints(tmp_path: Path) -> None:
    config_path = tmp_path / "parity.toml"
    config_path.write_text(
        """
version = 1

[[cases]]
name = "mixed-runtime"
fixture = "fixture.arrow"

[cases.reference]
command = ["./reference-target", "--mode=legacy"]
environment = { TARGET_PROFILE = "reference" }
record_distributions = ["pyarrow"]
native_threads = 1

[cases.candidate]
target = "candidate:transform"
canonicalizer = "candidate:canonicalize"
adapter = "arrow"
python = ".candidate/bin/python"
environment = { TARGET_PROFILE = "candidate" }
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    case = config.cases[0]

    assert case.reference.command == ["./reference-target", "--mode=legacy"]
    assert case.reference.target is None
    assert case.reference.workdir == tmp_path
    assert case.reference.environment == {"TARGET_PROFILE": "reference"}
    assert case.reference.record_distributions == ["pyarrow"]
    assert case.reference.native_threads == 1
    assert case.candidate.target == "candidate:transform"
    assert case.candidate.canonicalizer == "candidate:canonicalize"
    assert case.candidate.command is None
    assert case.candidate.python == tmp_path / ".candidate/bin/python"
    assert case.candidate.workdir == tmp_path


def test_command_and_output_canonicalizer_survive_artifact_replay(
    tmp_path: Path, monkeypatch
) -> None:
    _write_protocol_target(tmp_path / "reference-target")
    (tmp_path / "candidate.py").write_text(
        """
import pyarrow as pa


def transform(table):
    return pa.table({"value": [table.column("value")[0].as_py() + 1], "temporary": [99]})


def canonicalize(table):
    return table.select(["value"])
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    _write_arrow(fixture, pa.table({"value": [7]}))
    monkeypatch.chdir(tmp_path)
    case = CaseConfig(
        name="mixed-runtime-replay",
        reference=CallableSpec(command=["./reference-target"], workdir=tmp_path),
        candidate=CallableSpec(
            target="candidate:transform",
            canonicalizer="candidate:canonicalize",
            adapter="arrow",
            workdir=tmp_path,
        ),
        fixture=fixture,
        generation=GenerationConfig(
            max_examples=1,
            search=False,
            adversarial_examples=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )

    result = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    assert result.status is Status.FAILED
    failure = result.cases[0].failures[0]
    assert failure.artifact is not None
    replay = json.loads((failure.artifact / "replay.json").read_text(encoding="utf-8"))
    assert replay["command"] == ["parity", "replay", "<artifact-path>"]
    assert replay["case"]["reference"]["command"] == ["./reference-target"]
    assert replay["case"]["reference"]["target"] is None
    assert replay["case"]["candidate"]["command"] is None
    assert replay["case"]["candidate"]["canonicalizer"] == "candidate:canonicalize"

    replayed = replay_artifact(failure.artifact)

    assert replayed.status is Status.FAILED
    assert replayed.cases[0].failures[0].finding_signature == failure.finding_signature
    assert replayed.cases[0].provenance is not None
    assert replayed.cases[0].provenance.verification == "verified"


def test_command_and_environment_credentials_never_enter_artifacts_or_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command_secret = "command-secret-7f2e"
    assignment_secret = "assignment-secret-9a4d"
    environment_secret = "environment-secret-3c8b"
    _write_protocol_target(tmp_path / "reference-target")
    (tmp_path / "candidate.py").write_text(
        """
import pyarrow as pa


def transform(table):
    return pa.table({"value": [table.column("value")[0].as_py() + 1]})
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "fixture.arrow"
    _write_arrow(fixture, pa.table({"value": [1]}))
    case = CaseConfig(
        name="redacted-command",
        reference=CallableSpec(
            command=[
                "./reference-target",
                "--api-key",
                command_secret,
                f"SERVICE_TOKEN={assignment_secret}",
            ],
            workdir=tmp_path,
            environment={
                "AUTHORIZATION": environment_secret,
                "PRIVATE_TOKEN": "private-token-value",
            },
        ),
        candidate=CallableSpec(
            target="candidate:transform",
            adapter="arrow",
            workdir=tmp_path,
        ),
        fixture=fixture,
        generation=GenerationConfig(
            max_examples=1,
            search=False,
            adversarial_examples=False,
            stability_repeats=1,
        ),
        performance=PerformanceConfig(enabled=False),
    )
    monkeypatch.chdir(tmp_path)
    result = run_suite(ParityConfig(artifact_dir=tmp_path / ".parity", cases=[case]))

    assert result.status is Status.FAILED
    destination = result.cases[0].failures[0].artifact
    assert destination is not None

    replay_text = (destination / "replay.json").read_text(encoding="utf-8")
    replay = json.loads(replay_text)
    assert replay["case"]["reference"]["command"] == [
        "./reference-target",
        "--api-key",
        "<redacted>",
        "SERVICE_TOKEN=<redacted>",
    ]
    assert replay["case"]["reference"]["environment"] == {
        "AUTHORIZATION": "<required-from-environment>",
        "PRIVATE_TOKEN": "<required-from-environment>",
    }
    assert "command" not in replay

    with pytest.raises(ReplayError, match="redacted target configuration"):
        replay_artifact(destination)

    report_text = "\n".join(
        (
            render_json(result),
            render_markdown(result),
            render_terminal(result),
        )
    )
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in destination.glob("*.json"))
    for secret in (
        command_secret,
        assignment_secret,
        environment_secret,
        "private-token-value",
    ):
        assert secret not in replay_text
        assert secret not in persisted
        assert secret not in report_text


def test_replay_rejects_command_path_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workdir = project / "work"
    workdir.mkdir(parents=True)
    outside = tmp_path / "outside-target"
    outside.write_text("not executable by the test\n", encoding="utf-8")
    case_data = {
        "reference": {"workdir": "work", "command": ["../../outside-target"]},
        "candidate": {"workdir": "work", "command": ["safe-on-path"]},
    }

    with pytest.raises(ReplayError, match="command paths"):
        engine._resolve_replay_paths(case_data, project)


def test_replay_rejects_command_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-target"
    outside.write_text("external command\n", encoding="utf-8")
    linked = project / "linked-target"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not permit symlink creation")
    case_data = {
        "reference": {"workdir": ".", "command": ["./linked-target"]},
        "candidate": {"workdir": ".", "command": ["safe-on-path"]},
    }

    with pytest.raises(ReplayError, match="command paths"):
        engine._resolve_replay_paths(case_data, project)
