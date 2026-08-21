#!/usr/bin/env python3
"""Parity target-protocol adapter for the compiled Fortran reference."""

from __future__ import annotations

import json
import math
import os
import platform
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
SESSION_ROOT = Path(sys.argv[-1]).resolve(strict=True)
PROGRAM = (Path(__file__).resolve().parent / "bin" / "compensated_sum").resolve()


def _runtime() -> dict[str, Any]:
    return {
        "executor": "command",
        "runtime_name": "fortran",
        "runtime_version": "2008",
        "python_implementation": None,
        "python_version": None,
        "platform_system": platform.system() or "unknown",
        "platform_machine": platform.machine() or "unknown",
        "parity_version": None,
        "distributions": [],
        "identities": [],
    }


def _base_response(started: float) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "outcome": "returned",
        "duration_seconds": time.perf_counter() - started,
        "exception": None,
        "mutated_inputs": [],
        "return_type": None,
        "runtime": _runtime(),
        "output": None,
    }


def _error_response(started: float) -> dict[str, Any]:
    response = _base_response(started)
    response.update(
        outcome="error",
        exception={
            "module": "fortran_adapter",
            "type": "AdapterError",
            "message": "Fortran adapter could not complete the request",
            "details": {"error_codes": ["adapter_failure"]},
        },
    )
    return response


def _call_directory(token: str) -> Path:
    if not token or Path(token).name != token or token in {".", ".."}:
        raise ValueError("invalid call token")
    lexical = SESSION_ROOT / token
    if lexical.is_symlink():
        raise ValueError("call directory cannot be a symlink")
    resolved = lexical.resolve(strict=True)
    if resolved.parent != SESSION_ROOT or not resolved.is_dir():
        raise ValueError("call directory must be an immediate session child")
    return resolved


def _read_request(call_directory: Path) -> dict[str, Any]:
    source = call_directory / "request.json"
    metadata = source.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_REQUEST_BYTES
    ):
        raise ValueError("request is not a bounded single-linked regular file")
    parsed = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("request must be a JSON object")
    return parsed


def _declared_path(raw: object, call_directory: Path) -> Path:
    if not isinstance(raw, str):
        raise ValueError("protocol path must be a string")
    path = Path(raw)
    if not path.is_absolute() or path.parent.resolve(strict=True) != call_directory:
        raise ValueError("protocol path must be an immediate call child")
    return path


def _inspect_program() -> None:
    metadata = PROGRAM.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Fortran program must be a single-linked regular file")
    if not os.access(PROGRAM, os.X_OK):
        raise ValueError("Fortran program is not executable")


def _input_values(request: dict[str, Any], call_directory: Path) -> list[float]:
    inputs = request.get("inputs")
    if not isinstance(inputs, dict) or inputs.get("kind") != "single":
        raise ValueError("adapter requires one input")
    items = inputs.get("items")
    if not isinstance(items, list) or len(items) != 1 or items[0].get("name") != "input":
        raise ValueError("adapter requires one input named input")
    source = _declared_path(items[0].get("path"), call_directory)
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("input must be a single-linked regular file")
    with source.open("rb") as stream:
        table = ipc.open_file(stream).read_all()
    if table.column_names != ["value"] or not 1 <= table.num_rows <= 6:
        raise ValueError("input must contain one to six value rows")
    if table.schema.field("value").type != pa.float64():
        raise ValueError("value must use Arrow float64")
    values = table.column("value").to_pylist()
    if any(value is None or not math.isfinite(value) for value in values):
        raise ValueError("value must contain only finite non-null numbers")
    return values


def _write_json_output(destination: Path, value: float) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def _execute(request: dict[str, Any], call_directory: Path, started: float) -> dict[str, Any]:
    values = _input_values(request, call_directory)
    payload = f"{len(values)}\n" + "".join(f"{value:.17e}\n" for value in values)
    target_started = time.perf_counter()
    completed = subprocess.run(
        [str(PROGRAM)],
        input=payload,
        text=True,
        encoding="ascii",
        capture_output=True,
        timeout=5,
        check=False,
    )
    target_duration = time.perf_counter() - target_started
    tokens = completed.stdout.split()
    if completed.returncode != 0 or len(tokens) != 1:
        raise RuntimeError("Fortran program returned an invalid result")
    result = float(tokens[0])
    if not math.isfinite(result):
        raise RuntimeError("Fortran program returned a non-finite result")
    output = request.get("output")
    if not isinstance(output, dict):
        raise ValueError("request has no output declaration")
    destination = _declared_path(output.get("json"), call_directory)
    _write_json_output(destination, result)
    response = _base_response(started)
    response.update(
        duration_seconds=target_duration,
        return_type="fortran.real64",
        output={"kind": "json"},
    )
    return response


def _respond(request: dict[str, Any], call_directory: Path, started: float) -> dict[str, Any]:
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    operation = request.get("operation")
    if operation == "runtime":
        return _base_response(started)
    if operation == "inspect":
        _inspect_program()
        return _base_response(started)
    if operation == "execute":
        return _execute(request, call_directory, started)
    raise ValueError("unsupported protocol operation")


def _write_response(call_directory: Path, response: dict[str, Any]) -> None:
    destination = call_directory / "response.json"
    temporary = call_directory / ".response.json.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(response, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def main() -> None:
    for raw_token in sys.stdin.buffer:
        token = raw_token.rstrip(b"\r\n").decode("ascii")
        call_directory = _call_directory(token)
        started = time.perf_counter()
        try:
            request = _read_request(call_directory)
            response = _respond(request, call_directory, started)
        except Exception:
            response = _error_response(started)
        _write_response(call_directory, response)


if __name__ == "__main__":
    main()
