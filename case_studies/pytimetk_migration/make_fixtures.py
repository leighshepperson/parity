"""Regenerate the small, synthetic Arrow fixtures used by the PyTimeTK pilot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

FIXTURES = Path(__file__).with_name("fixtures")
TIMESTAMP = pa.timestamp("ns")


def _timestamps(values: list[str | None]) -> pa.Array:
    parsed = [datetime.fromisoformat(value) if value is not None else None for value in values]
    return pa.array(parsed, type=TIMESTAMP)


def _write(name: str, table: pa.Table) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    with path.open("wb") as stream, ipc.new_file(stream, table.schema) as writer:
        writer.write_table(table)


def build_fixtures() -> None:
    _write(
        "augment_unsorted.arrow",
        pa.table(
            {
                "row_id": pa.array(range(8), type=pa.int64()),
                "group": pa.array(["A", "B", "A", "B", "A", "B", "A", "B"]),
                "date": _timestamps(
                    [
                        "2024-01-03",
                        "2024-01-02",
                        "2024-01-01",
                        "2024-01-01",
                        "2024-01-02",
                        "2024-01-03",
                        "2024-01-04",
                        "2024-01-04",
                    ]
                ),
                "value": pa.array([3.0, 20.0, 1.0, 10.0, 2.0, 30.0, 4.0, 40.0]),
                "volume": pa.array([30.0, 200.0, 10.0, 100.0, 20.0, 300.0, 40.0, 400.0]),
            }
        ),
    )

    _write(
        "augment_hostile.arrow",
        pa.table(
            {
                "row_id": pa.array(range(10), type=pa.int64()),
                "group": pa.array(["A", None, "A", "B", "A", None, "A", "B", "B", "B"]),
                "date": _timestamps(
                    [
                        "2024-01-03",
                        "2024-01-02",
                        "2024-01-01",
                        "2024-01-01",
                        None,
                        "2024-01-03",
                        "2024-01-02",
                        None,
                        "2024-01-02",
                        "2024-01-03",
                    ]
                ),
                "value": pa.array(
                    [3.0, 20.0, 1.0, 10.0, None, 30.0, 4.0, 90.0, None, 30.0],
                    type=pa.float64(),
                ),
                "volume": pa.array(
                    [30.0, 200.0, 10.0, 100.0, 999.0, 300.0, 40.0, 900.0, 20.0, 300.0]
                ),
            }
        ),
    )

    _write(
        "pad.arrow",
        pa.table(
            {
                "series": pa.array(["S", "S", "S"]),
                "date": _timestamps(["2024-01-01", "2024-01-03", "2024-01-06"]),
                "value": pa.array([1.0, 3.0, 6.0]),
                "constant": pa.array(["x", "x", "x"]),
            }
        ),
    )

    _write(
        "pad_grouped.arrow",
        pa.table(
            {
                "group": pa.array(["A", "A", "A", "B", "B"]),
                "date": _timestamps(
                    ["2024-01-01", "2024-01-03", "2024-01-06", "2024-01-02", "2024-01-05"]
                ),
                "value": pa.array([1.0, 3.0, 6.0, 2.0, 5.0]),
                "constant": pa.array(["x", "x", "x", "y", "y"]),
            }
        ),
    )

    _write(
        "pad_numeric_hostile.arrow",
        pa.table(
            {
                "group": pa.array(["A", "A", "A", "B", "B", "B", None, None]),
                "date": _timestamps(
                    [
                        "2024-01-01",
                        "2024-01-03",
                        None,
                        "2024-01-02",
                        "2024-01-05",
                        None,
                        "2024-01-01",
                        "2024-01-03",
                    ]
                ),
                "value": pa.array([1.0, 3.0, 99.0, 2.0, 5.0, 90.0, 20.0, 30.0]),
                "count": pa.array([1, 3, 99, 2, 5, 90, 20, 30], type=pa.int64()),
            }
        ),
    )


if __name__ == "__main__":
    build_fixtures()
