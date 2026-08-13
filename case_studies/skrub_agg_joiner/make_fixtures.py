"""Generate the Arrow fixtures whose null/NaN distinction JSON cannot encode."""

from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def write_arrow(name: str, table: pa.Table) -> None:
    """Write one deterministic Arrow IPC file."""

    with ipc.new_file(FIXTURE_DIR / name, table.schema) as writer:
        writer.write_table(table)


write_arrow(
    "arrow_null.arrow",
    pa.table(
        {
            "key": pa.array(["A", "A", "B", "B"]),
            "value": pa.array([1.0, None, None, 2.0], type=pa.float64()),
        }
    ),
)

write_arrow(
    "ieee_nan.arrow",
    pa.table(
        {
            "key": pa.array(["A", "A", "B", "B"]),
            "value": pa.array(
                [1.0, float("nan"), float("nan"), 2.0],
                type=pa.float64(),
                from_pandas=False,
            ),
        }
    ),
)
