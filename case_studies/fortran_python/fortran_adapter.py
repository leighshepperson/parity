"""Domain adapter for the compiled Fortran compensated-sum reference."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pyarrow as pa

from parity.target_adapter import (
    AdapterError,
    CommandAdapter,
    RuntimeInfo,
    require_executable,
)

PROGRAM = Path(__file__).resolve().parent / "bin" / "compensated_sum"


def _inspect_program() -> None:
    require_executable(PROGRAM)


def _input_values(table: pa.Table) -> list[float]:
    if table.column_names != ["value"] or not 1 <= table.num_rows <= 6:
        raise AdapterError(
            "invalid_canonical_input",
            "input must contain one to six value rows",
        )
    if table.schema.field("value").type != pa.float64():
        raise AdapterError("invalid_canonical_input", "value must use Arrow float64")
    values = table.column("value").to_pylist()
    if any(value is None or not math.isfinite(value) for value in values):
        raise AdapterError(
            "invalid_canonical_input",
            "value must contain only finite non-null numbers",
        )
    return values


def _execute(table: pa.Table) -> float:
    values = _input_values(table)
    payload = f"{len(values)}\n" + "".join(f"{value:.17e}\n" for value in values)
    try:
        completed = subprocess.run(
            [str(PROGRAM)],
            input=payload,
            text=True,
            encoding="ascii",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError(
            "target_execution_failed",
            "Fortran program could not be executed",
        ) from error

    tokens = completed.stdout.split()
    if completed.returncode != 0 or len(tokens) != 1:
        raise AdapterError(
            "invalid_target_output",
            "Fortran program returned an invalid result",
        )
    try:
        result = float(tokens[0])
    except ValueError as error:
        raise AdapterError(
            "invalid_target_output",
            "Fortran program returned an invalid result",
        ) from error
    if not math.isfinite(result):
        raise AdapterError(
            "invalid_target_output",
            "Fortran program returned a non-finite result",
        )
    return result


adapter = CommandAdapter(
    runtime=RuntimeInfo(name="fortran", version="2008"),
    inspect=_inspect_program,
    execute=_execute,
    return_type="fortran.real64",
)
