"""Data-safe direct reproduction of the two PyIndicators EMA outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
from pyindicators_parity import pandas_ema, polars_ema

ROOT = Path(__file__).parent


def _observe(function: Callable[[Any], Any], frame: Any) -> dict[str, object]:
    try:
        result = function(frame)
    except Exception as error:
        return {
            "outcome": "raised",
            "exception_module": type(error).__module__,
            "exception_type": type(error).__qualname__,
        }
    return {
        "outcome": "returned",
        "columns": list(result.columns),
        "row_count": len(result),
    }


def main() -> None:
    summary: dict[str, dict[str, dict[str, object]]] = {}
    for fixture_name in ("finite", "nullable"):
        rows = json.loads((ROOT / "fixtures" / f"{fixture_name}.json").read_text())
        summary[fixture_name] = {
            "pandas": _observe(pandas_ema, pd.DataFrame(rows)),
            "polars": _observe(polars_ema, pl.DataFrame(rows)),
        }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
