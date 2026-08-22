"""Domain adapter for the compiled C++ order-book reference."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pyarrow as pa

from parity.target_adapter import (
    AdapterError,
    CommandAdapter,
    RuntimeInfo,
    TargetRaised,
    require_executable,
)

PROGRAM = Path(__file__).resolve().parent / "bin" / "legacy_orderbook"
OUTPUT_SCHEMA = pa.schema(
    [
        ("trade_sequence", pa.int64()),
        ("instrument", pa.string()),
        ("maker_order_id", pa.int64()),
        ("taker_order_id", pa.int64()),
        ("buyer_order_id", pa.int64()),
        ("seller_order_id", pa.int64()),
        ("price_ticks", pa.int64()),
        ("quantity_lots", pa.int64()),
        ("quantity_units", pa.int64()),
    ]
)


def _inspect() -> None:
    require_executable(PROGRAM)


def _payload(events: pa.Table, instruments: pa.Table) -> str:
    lines = [f"I {instruments.num_rows}"]
    for row in instruments.to_pylist():
        lines.append(f"{row['instrument']} {row['lot_size']}")
    lines.append(f"E {events.num_rows}")
    for row in events.to_pylist():
        lines.append(
            " ".join(
                str(row[name])
                for name in (
                    "sequence",
                    "event_type",
                    "order_id",
                    "instrument",
                    "side",
                    "price_ticks",
                    "quantity_lots",
                )
            )
        )
    return "\n".join(lines) + "\n"


def _execute(events: pa.Table, instruments: pa.Table) -> pa.Table:
    try:
        completed = subprocess.run(
            [str(PROGRAM)],
            input=_payload(events, instruments),
            text=True,
            encoding="ascii",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError(
            "target_invocation_failed",
            "legacy order-book engine could not be invoked",
        ) from error

    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or not lines:
        raise AdapterError(
            "invalid_target_output",
            "legacy order-book engine returned an unusable response",
        )
    if lines[0] == "ERROR inactive_cancel":
        raise TargetRaised(
            "cannot cancel an inactive order",
            module="legacy.exchange",
            exception_type="InvalidCancel",
        )

    header = lines[0].split()
    if len(header) != 2 or header[0] != "OK":
        raise AdapterError("invalid_target_output", "legacy response header is invalid")
    try:
        row_count = int(header[1])
    except ValueError as error:
        raise AdapterError("invalid_target_output", "legacy response count is invalid") from error
    if row_count != len(lines) - 1:
        raise AdapterError("invalid_target_output", "legacy response count does not match")

    rows: list[dict[str, object]] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 9:
            raise AdapterError("invalid_target_output", "legacy fill row is invalid")
        try:
            rows.append(
                {
                    "trade_sequence": int(fields[0]),
                    "instrument": fields[1],
                    "maker_order_id": int(fields[2]),
                    "taker_order_id": int(fields[3]),
                    "buyer_order_id": int(fields[4]),
                    "seller_order_id": int(fields[5]),
                    "price_ticks": int(fields[6]),
                    "quantity_lots": int(fields[7]),
                    "quantity_units": int(fields[8]),
                }
            )
        except ValueError as error:
            raise AdapterError("invalid_target_output", "legacy fill value is invalid") from error
    return pa.Table.from_pylist(rows, schema=OUTPUT_SCHEMA)


adapter = CommandAdapter(
    runtime=RuntimeInfo(name="cpp", version="cxx17"),
    inspect=_inspect,
    execute=_execute,
    return_type="arrow.table",
)
