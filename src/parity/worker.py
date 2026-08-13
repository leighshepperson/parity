"""Private isolated-execution worker.

The parent supplies two paths.  Data travels only through private Arrow/JSON
files; stdout and stderr are never part of the protocol.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from parity.execution import (
    _WORKER_PROTOCOL_VERSION,
    ExecutionOutcome,
    Observation,
    _read_input_bundle,
    _write_arrow,
    execute_current,
)
from parity.models import CallableSpec, RunMetrics
from parity.provenance import RuntimeProvenance, collect_runtime_provenance


def run_request(request_path: Path, response_path: Path) -> None:
    root = request_path.parent.resolve(strict=True)
    if request_path.resolve(strict=True) != root / "request.json":
        raise ValueError("invalid worker request path")
    if response_path.resolve() != root / "response.json":
        raise ValueError("invalid worker response path")
    request_raw: Any = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request_raw, dict):
        raise ValueError("invalid worker request")
    request: dict[str, Any] = request_raw
    if request.get("protocol_version") != _WORKER_PROTOCOL_VERSION:
        raise ValueError("unsupported worker protocol")
    raw_spec = request["spec"]
    spec = CallableSpec(
        target=raw_spec["target"],
        adapter=raw_spec.get("adapter", "auto"),
        pandas_input=raw_spec.get("pandas_input", "arrow"),
        workdir=raw_spec.get("workdir"),
        record_distributions=raw_spec.get("record_distributions", []),
    )
    expected_raw = request.get("expected_runtime")
    expected_runtime = (
        RuntimeProvenance.model_validate(expected_raw) if expected_raw is not None else None
    )
    inputs = _read_input_bundle(request.get("inputs"), request_path.parent)
    operation = request.get("operation", "execute")
    static_args = request.get("static_args", [])
    static_kwargs = request.get("static_kwargs", {})
    if not isinstance(static_args, list):
        raise ValueError("worker static_args must be a list")
    if not isinstance(static_kwargs, dict) or not all(
        isinstance(name, str) for name in static_kwargs
    ):
        raise ValueError("worker static_kwargs must be an object with string keys")
    raw_output_arrow = request.get("output_arrow")
    raw_output_json = request.get("output_json")
    if not isinstance(raw_output_arrow, str) or not isinstance(raw_output_json, str):
        raise ValueError("invalid worker output paths")
    output_arrow = Path(raw_output_arrow).resolve()
    output_json = Path(raw_output_json).resolve()
    if output_arrow != root / "output.arrow" or output_json != root / "output.json":
        raise ValueError("worker output paths escape their call directory")
    if operation == "provenance":
        observation = Observation(
            outcome=ExecutionOutcome.RETURNED,
            metrics=RunMetrics(duration_seconds=0),
            runtime=collect_runtime_provenance(spec.record_distributions),
        )
    elif operation == "execute":
        observation = execute_current(
            spec,
            inputs.as_public_bundle(),
            static_args=static_args,
            static_kwargs=static_kwargs,
            expected_runtime=expected_runtime,
        )
    else:
        raise ValueError("unsupported worker operation")
    if observation.table is not None:
        _write_arrow(observation.table, output_arrow)
    if observation.has_value:
        output_json.write_text(json.dumps(observation.value, allow_nan=True), encoding="utf-8")
    payload = observation.to_metadata()
    payload["protocol_version"] = _WORKER_PROTOCOL_VERSION
    temporary = response_path.with_name(f".{response_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, response_path)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        return 2
    try:
        run_request(Path(arguments[0]), Path(arguments[1]))
    except BaseException:
        # Do not print exceptions: they can contain source, paths, environment,
        # or input data.  The parent turns this status into WorkerError.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
