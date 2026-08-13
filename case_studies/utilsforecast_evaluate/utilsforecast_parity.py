"""Pandas and Polars evaluation targets for utilsforecast 0.2.16."""

from __future__ import annotations

import pandas as pd
import polars as pl
from utilsforecast.evaluation import evaluate
from utilsforecast.losses import mae, rmse


def pandas_evaluate(frame: pd.DataFrame) -> pd.DataFrame:
    """Evaluate one model through utilsforecast's pandas path."""

    return evaluate(frame, metrics=[mae, rmse], models=["model"])


def polars_evaluate(frame: pl.DataFrame) -> pl.DataFrame:
    """Evaluate one model through utilsforecast's Polars path."""

    return evaluate(frame, metrics=[mae, rmse], models=["model"])
