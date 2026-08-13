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

from parity.execution import _read_arrow, _write_arrow, execute_current
from parity.models import CallableSpec


def run_request(request_path: Path, response_path: Path) -> None:
    request: dict[str, Any] = json.loads(request_path.read_text(encoding="utf-8"))
    raw_spec = request["spec"]
    spec = CallableSpec(
        target=raw_spec["target"],
        adapter=raw_spec.get("adapter", "auto"),
        workdir=raw_spec.get("workdir"),
    )
    observation = execute_current(
        spec,
        _read_arrow(Path(request["input"])),
        static_args=request.get("static_args", []),
        static_kwargs=request.get("static_kwargs", {}),
    )
    if observation.table is not None:
        _write_arrow(observation.table, Path(request["output_arrow"]))
    if observation.has_value:
        Path(request["output_json"]).write_text(
            json.dumps(observation.value, allow_nan=True), encoding="utf-8"
        )
    payload = observation.to_metadata()
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
        # or customer data.  The parent turns this status into WorkerError.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
