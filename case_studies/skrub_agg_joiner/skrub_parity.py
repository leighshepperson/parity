"""Cross-backend wrappers around skrub's pinned aggregation implementation."""

from __future__ import annotations

import pandas as pd
import polars as pl
from skrub import AggJoiner
from skrub._agg_joiner import aggregate

NUMERIC_OPERATIONS = ["count", "min", "max", "sum", "median", "mean", "std"]
BASIC_OPERATIONS = ["count", "sum", "mean"]


def pandas_aggregate_numeric(frame: pd.DataFrame) -> pd.DataFrame:
    """Exercise the direct pandas aggregation implementation."""

    return aggregate(frame, ["key"], ["value"], NUMERIC_OPERATIONS, "")


def polars_aggregate_numeric(frame: pl.DataFrame) -> pl.DataFrame:
    """Exercise the equivalent direct Polars aggregation implementation."""

    return aggregate(frame, ["key"], ["value"], NUMERIC_OPERATIONS, "")


def pandas_aggregate_basic(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate count, sum and mean using pandas."""

    return aggregate(frame, ["key"], ["value"], BASIC_OPERATIONS, "")


def polars_aggregate_basic(frame: pl.DataFrame) -> pl.DataFrame:
    """Aggregate count, sum and mean using Polars."""

    return aggregate(frame, ["key"], ["value"], BASIC_OPERATIONS, "")


def pandas_aggregate_mode(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a string column with skrub's pandas mode implementation."""

    return aggregate(frame, ["key"], ["label"], ["mode"], "")


def polars_aggregate_mode(frame: pl.DataFrame) -> pl.DataFrame:
    """Aggregate a string column with skrub's Polars mode implementation."""

    return aggregate(frame, ["key"], ["label"], ["mode"], "")


def pandas_aggjoiner_numeric(frame: pd.DataFrame) -> pd.DataFrame:
    """Run the public pandas AggJoiner self-aggregation path."""

    return AggJoiner(
        aux_table="X",
        operations=BASIC_OPERATIONS,
        key="key",
        cols="value",
    ).fit_transform(frame)


def polars_aggjoiner_numeric(frame: pl.DataFrame) -> pl.DataFrame:
    """Run the public Polars AggJoiner self-aggregation path."""

    return AggJoiner(
        aux_table="X",
        operations=BASIC_OPERATIONS,
        key="key",
        cols="value",
    ).fit_transform(frame)


def pandas_aggjoiner_mode(frame: pd.DataFrame) -> pd.DataFrame:
    """Run the public pandas AggJoiner mode path."""

    return AggJoiner(
        aux_table="X",
        operations="mode",
        key="key",
        cols="label",
    ).fit_transform(frame)


def polars_aggjoiner_mode(frame: pl.DataFrame) -> pl.DataFrame:
    """Run the public Polars AggJoiner mode path."""

    return AggJoiner(
        aux_table="X",
        operations="mode",
        key="key",
        cols="label",
    ).fit_transform(frame)
