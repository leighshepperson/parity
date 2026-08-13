"""Backend-specific calls to the public PyIndicators EMA function."""

from __future__ import annotations

import pandas as pd
import polars as pl
from pyindicators import ema


def pandas_ema(frame: pd.DataFrame) -> pd.DataFrame:
    """Run EMA on a copy so the study compares results, not wrapper mutation."""

    result = ema(
        frame.copy(deep=True),
        source_column="price",
        period=3,
        result_column="ema",
    )
    return result[["row_id", "ema"]]


def polars_ema(frame: pl.DataFrame) -> pl.DataFrame:
    """Run the same public EMA function through its Polars branch."""

    result = ema(
        frame.clone(),
        source_column="price",
        period=3,
        result_column="ema",
    )
    return result.select("row_id", "ema")
