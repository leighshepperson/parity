"""Data-safe summary of the pandas categorical group-by result."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from pandas_version_parity import categorical_total

ROOT = Path(__file__).parent


def main() -> None:
    rows = json.loads((ROOT / "fixtures" / "observed.json").read_text())
    result = categorical_total(pd.DataFrame(rows))
    print(
        json.dumps(
            {
                "columns": list(result.columns),
                "outcome": "returned",
                "pandas_version": pd.__version__,
                "row_count": len(result),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
