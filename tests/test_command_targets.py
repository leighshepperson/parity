from __future__ import annotations

import os
import sys
from pathlib import Path

import pyarrow as pa
import pytest

import parity.execution as execution_module
from parity.execution import ExecutionOutcome, IsolatedExecutionSession, execute_isolated
from parity.models import CallableSpec


def _command_worker(tmp_path: Path) -> Path:
    worker = tmp_path / "command_target.py"
    worker.write_text(
        """
import json
import os
import platform
import sys
import time

import pyarrow as pa
import pyarrow.ipc as ipc

root = os.path.realpath(sys.argv[-1])
delta = int(sys.argv[1]) if len(sys.argv) == 3 else 0

def runtime():
    return {
        "executor": "command",
        "runtime_name": "test-command",
        "runtime_version": "1.0",
        "python_implementation": None,
        "python_version": None,
        "platform_system": platform.system() or "unknown",
        "platform_machine": platform.machine() or "unknown",
        "parity_version": None,
        "distributions": [],
    }

def respond(call_root, request):
    started = time.perf_counter()
    common = {
        "protocol_version": 1,
        "duration_seconds": 0.0,
        "mutated_inputs": [],
        "return_type": None,
        "runtime": runtime(),
        "output": None,
    }
    if request["operation"] in {"runtime", "inspect"}:
        common.update(outcome="returned", exception=None)
        return common
    source = request["inputs"]["items"][0]["path"]
    with open(source, "rb") as stream:
        table = ipc.open_file(stream).read_all()
    mode = table.column("mode")[0].as_py() if "mode" in table.column_names else "return"
    if mode == "raise":
        common.update(
            outcome="raised",
            exception={
                "module": "legacy.system",
                "type": "DomainRejected",
                "message": "record 7849231 was rejected",
                "details": {},
            },
        )
    elif mode == "error":
        common.update(
            outcome="error",
            exception={
                "module": "parity.target",
                "type": "TargetProtocolError",
                "message": "command adapter failed",
                "details": {},
            },
        )
    elif mode in {"json", "symlink-json"}:
        value = {"value": "x" * 256}
        destination = request["output"]["json"]
        if mode == "symlink-json":
            outside = os.path.join(root, "outside-" + os.path.basename(call_root) + ".json")
            with open(outside, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
            os.symlink(outside, destination)
        else:
            with open(destination, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
        common.update(
            outcome="returned",
            exception=None,
            return_type="test.command.JsonResult",
            output={"kind": "json"},
        )
    else:
        result = pa.table({"value": [table.column("value")[0].as_py() + delta]})
        destination = request["output"]["arrow"]
        with open(destination, "wb") as stream, ipc.new_file(stream, result.schema) as writer:
            writer.write_table(result)
        common.update(
            outcome="returned",
            exception=None,
            return_type="test.command.Result",
            output={"kind": "arrow"},
        )
    common["duration_seconds"] = time.perf_counter() - started
    return common

for raw_token in sys.stdin.buffer:
    token = raw_token.rstrip(b"\\r\\n").decode("ascii")
    call_root = os.path.join(root, token)
    with open(os.path.join(call_root, "request.json"), encoding="utf-8") as stream:
        request = json.load(stream)
    response = respond(call_root, request)
    destination = os.path.join(call_root, "response.json")
    fault = os.environ.get("PARITY_TEST_PROTOCOL_FAULT")
    if fault == "response-symlink":
        outside = os.path.join(root, "outside-" + token + ".json")
        with open(outside, "w", encoding="utf-8") as stream:
            json.dump(response, stream, sort_keys=True)
        os.symlink(outside, destination)
    elif fault == "response-directory":
        os.mkdir(destination)
    elif fault == "response-oversized":
        temporary = destination + ".tmp"
        with open(temporary, "wb") as stream:
            stream.write(b"x" * (1024 * 1024 + 1))
        os.replace(temporary, destination)
    elif fault == "response-link-delay":
        temporary = destination + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(response, stream, sort_keys=True)
        os.link(temporary, destination)
        time.sleep(0.05)
        os.unlink(temporary)
    elif fault == "response-hardlink":
        outside = os.path.join(root, "outside-" + token + ".json")
        with open(outside, "w", encoding="utf-8") as stream:
            json.dump(response, stream, sort_keys=True)
        os.link(outside, destination)
    else:
        temporary = destination + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(response, stream, sort_keys=True)
        os.replace(temporary, destination)
""".lstrip(),
        encoding="utf-8",
    )
    return worker


def _spec(tmp_path: Path, *, delta: int = 0, fault: str | None = None) -> CallableSpec:
    return CallableSpec(
        command=[sys.executable, str(_command_worker(tmp_path)), str(delta)],
        workdir=tmp_path,
        environment=({"PARITY_TEST_PROTOCOL_FAULT": fault} if fault is not None else {}),
    )


def test_command_target_returns_canonical_arrow_without_parity_import(tmp_path: Path) -> None:
    observation = execute_isolated(
        _spec(tmp_path, delta=2),
        pa.table({"mode": ["return"], "value": [40]}),
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.table == pa.table({"value": [42]})
    assert observation.runtime is not None
    assert observation.runtime.executor == "command"
    assert observation.runtime.runtime_name == "test-command"
    assert observation.runtime.python_version is None


def test_command_target_raise_is_semantic_but_adapter_error_is_infrastructure(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    raised = execute_isolated(
        spec,
        pa.table({"mode": ["raise"], "value": [1]}),
        timeout_seconds=5,
    )
    errored = execute_isolated(
        spec,
        pa.table({"mode": ["error"], "value": [1]}),
        timeout_seconds=5,
    )

    assert raised.outcome is ExecutionOutcome.RAISED
    assert raised.exception is not None
    assert raised.exception.type == "DomainRejected"
    assert errored.outcome is ExecutionOutcome.ERROR
    assert errored.exception is not None
    assert errored.exception.type == "TargetProtocolError"


def test_command_session_preflights_once_and_preserves_process_state(tmp_path: Path) -> None:
    spec = _spec(tmp_path, delta=1)
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        preflight = session.preflight_runtime()
        first = session.execute(pa.table({"mode": ["return"], "value": [1]}))
        second = session.execute(pa.table({"mode": ["return"], "value": [2]}))

    assert preflight.outcome is ExecutionOutcome.RETURNED
    assert first.table == pa.table({"value": [2]})
    assert second.table == pa.table({"value": [3]})


def test_command_target_environment_does_not_inherit_controller_pythonpath(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/controller/private/site-packages")
    spec = _spec(tmp_path)
    assert "PYTHONPATH" not in __import__("parity.execution", fromlist=["x"])._isolated_environment(
        spec
    )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not generally available")
def test_command_protocol_rejects_symlink_response(tmp_path: Path) -> None:
    with IsolatedExecutionSession(
        _spec(tmp_path, fault="response-symlink"), timeout_seconds=5
    ) as session:
        observation = session.inspect_runtime()

    assert observation.outcome is ExecutionOutcome.CRASHED
    assert observation.exception is not None
    assert observation.exception.type == "WorkerProtocolError"


def test_command_protocol_rejects_non_regular_response(tmp_path: Path) -> None:
    with IsolatedExecutionSession(
        _spec(tmp_path, fault="response-directory"), timeout_seconds=5
    ) as session:
        observation = session.inspect_runtime()

    assert observation.outcome is ExecutionOutcome.CRASHED
    assert observation.exception is not None
    assert observation.exception.type == "WorkerProtocolError"


def test_command_protocol_hard_bounds_response_read(tmp_path: Path) -> None:
    with IsolatedExecutionSession(
        _spec(tmp_path, fault="response-oversized"), timeout_seconds=5
    ) as session:
        observation = session.inspect_runtime()

    assert observation.outcome is ExecutionOutcome.CRASHED
    assert observation.exception is not None
    assert observation.exception.type == "WorkerProtocolError"


def test_command_protocol_waits_for_atomic_hard_link_publication(tmp_path: Path) -> None:
    with IsolatedExecutionSession(
        _spec(tmp_path, fault="response-link-delay"), timeout_seconds=5
    ) as session:
        observation = session.inspect_runtime()

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.runtime is not None
    assert observation.runtime.runtime_name == "test-command"


def test_command_protocol_still_rejects_a_stable_hard_link(tmp_path: Path) -> None:
    with IsolatedExecutionSession(
        _spec(tmp_path, fault="response-hardlink"), timeout_seconds=1
    ) as session:
        observation = session.inspect_runtime()

    assert observation.outcome is ExecutionOutcome.CRASHED
    assert observation.exception is not None
    assert observation.exception.type == "WorkerProtocolError"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not generally available")
def test_command_protocol_rejects_symlink_output(tmp_path: Path) -> None:
    observation = execute_isolated(
        _spec(tmp_path),
        pa.table({"mode": ["symlink-json"], "value": [1]}),
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.CRASHED
    assert observation.exception is not None
    assert observation.exception.type == "WorkerProtocolError"


@pytest.mark.parametrize(
    ("mode", "limit_name"),
    [
        ("json", "_MAX_TARGET_JSON_OUTPUT_BYTES"),
        ("return", "_MAX_TARGET_ARROW_OUTPUT_BYTES"),
    ],
)
def test_command_protocol_hard_bounds_output_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    limit_name: str,
) -> None:
    monkeypatch.setattr(execution_module, limit_name, 32)

    observation = execute_isolated(
        _spec(tmp_path),
        pa.table({"mode": [mode], "value": [1]}),
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.CRASHED
    assert observation.exception is not None
    assert observation.exception.type == "WorkerProtocolError"
