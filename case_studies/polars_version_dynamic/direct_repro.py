"""Data-safe summary of the Polars dynamic-window result in one runtime."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
from polars_version_parity import dynamic_window

ROOT = Path(__file__).parent


def main() -> None:
    rows = json.loads((ROOT / "fixtures" / "boundary.json").read_text())
    result = dynamic_window(pl.DataFrame(rows))
    print(
        json.dumps(
            {
                "columns": result.columns,
                "outcome": "returned",
                "polars_version": pl.__version__,
                "row_count": result.height,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
