"""Sorted as-of and valid-interval targets for the frame-constraint study."""

from __future__ import annotations

import pandas as pd
import polars as pl


def pandas_backward(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    """Use pandas' backward as-of strategy on already-sorted inputs."""

    return pd.merge_asof(left, right, on="time", direction="backward")


def polars_forward(left: pl.DataFrame, right: pl.DataFrame) -> pl.DataFrame:
    """Deliberately use the opposite Polars as-of strategy."""

    return left.join_asof(right, on="time", strategy="forward")


def pandas_span(frame: pd.DataFrame) -> pd.DataFrame:
    """Compute a non-negative span over a row-valid interval."""

    result = frame.copy()
    result["span"] = result["end"] - result["start"]
    return result


def polars_span(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute the same span in Polars."""

    return frame.with_columns((pl.col("end") - pl.col("start")).alias("span"))
