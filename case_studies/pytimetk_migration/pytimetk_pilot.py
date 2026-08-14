"""Public-boundary wrappers for the bounded PyTimeTK backend migration pilot."""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytimetk as tk


def _pandas_group(frame: pd.DataFrame):
    """Group without discarding input rows before PyTimeTK receives the frame."""

    return frame.groupby("group", sort=False, dropna=False)


def _polars_group(frame: pl.DataFrame):
    """Use the corresponding stable first-seen Polars grouping contract."""

    return frame.group_by("group", maintain_order=True)


def lags_control_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_lags(
        frame,
        date_column="date",
        value_column=["value", "volume"],
        lags=[1, 3],
        engine="pandas",
    )


def lags_control_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_lags(
        frame,
        date_column="date",
        value_column=["value", "volume"],
        lags=[1, 3],
        engine="polars",
    )


def lags_grouped_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_lags(
        _pandas_group(frame),
        date_column="date",
        value_column="value",
        lags=(1, 2),
        engine="pandas",
    )


def lags_grouped_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_lags(
        _polars_group(frame),
        date_column="date",
        value_column="value",
        lags=(1, 2),
        engine="polars",
    )


def lags_null_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_lags(
        _pandas_group(frame),
        date_column="date",
        value_column="value",
        lags=1,
        engine="pandas",
    )


def lags_null_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_lags(
        _polars_group(frame),
        date_column="date",
        value_column="value",
        lags=1,
        engine="polars",
    )


def rolling_control_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_rolling(
        frame,
        date_column="date",
        value_column=["value", "volume"],
        window_func=["mean", "sum"],
        window=[2, 4],
        min_periods=1,
        show_progress=False,
        engine="pandas",
    )


def rolling_control_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_rolling(
        frame,
        date_column="date",
        value_column=["value", "volume"],
        window_func=["mean", "sum"],
        window=[2, 4],
        min_periods=1,
        show_progress=False,
        engine="polars",
    )


def rolling_grouped_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_rolling(
        _pandas_group(frame),
        date_column="date",
        value_column="value",
        window_func=["mean", "std"],
        window=4,
        min_periods=2,
        center=True,
        show_progress=False,
        engine="pandas",
    )


def rolling_grouped_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_rolling(
        _polars_group(frame),
        date_column="date",
        value_column="value",
        window_func=["mean", "std"],
        window=4,
        min_periods=2,
        center=True,
        show_progress=False,
        engine="polars",
    )


def rolling_null_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_rolling(
        _pandas_group(frame),
        date_column="date",
        value_column="value",
        window_func="mean",
        window=2,
        min_periods=1,
        show_progress=False,
        engine="pandas",
    )


def rolling_null_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_rolling(
        _polars_group(frame),
        date_column="date",
        value_column="value",
        window_func="mean",
        window=2,
        min_periods=1,
        show_progress=False,
        engine="polars",
    )


def ewm_control_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_ewm(
        frame,
        date_column="date",
        value_column="value",
        window_func=["mean", "std", "var"],
        alpha=[0.2, 0.7],
        adjust=False,
        min_periods=1,
        engine="pandas",
    )


def ewm_control_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_ewm(
        frame,
        date_column="date",
        value_column="value",
        window_func=["mean", "std", "var"],
        alpha=[0.2, 0.7],
        adjust=False,
        min_periods=1,
        engine="polars",
    )


def ewm_grouped_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_ewm(
        _pandas_group(frame),
        date_column="date",
        value_column="value",
        window_func="mean",
        alpha=0.3,
        adjust=False,
        engine="pandas",
    )


def ewm_grouped_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_ewm(
        _polars_group(frame),
        date_column="date",
        value_column="value",
        window_func="mean",
        alpha=0.3,
        adjust=False,
        engine="polars",
    )


def ewm_null_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_ewm(
        _pandas_group(frame),
        date_column="date",
        value_column="value",
        window_func=["mean", "std"],
        alpha=0.3,
        adjust=False,
        min_periods=1,
        engine="pandas",
    )


def ewm_null_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_ewm(
        _polars_group(frame),
        date_column="date",
        value_column="value",
        window_func=["mean", "std"],
        alpha=0.3,
        adjust=False,
        min_periods=1,
        engine="polars",
    )


def macd_control_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_macd(
        frame,
        date_column="date",
        close_column="value",
        fast_period=2,
        slow_period=4,
        signal_period=2,
        engine="pandas",
    )


def macd_control_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_macd(
        frame,
        date_column="date",
        close_column="value",
        fast_period=2,
        slow_period=4,
        signal_period=2,
        engine="polars",
    )


def macd_grouped_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_macd(
        _pandas_group(frame),
        date_column="date",
        close_column="value",
        fast_period=2,
        slow_period=5,
        signal_period=2,
        engine="pandas",
    )


def macd_grouped_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_macd(
        _polars_group(frame),
        date_column="date",
        close_column="value",
        fast_period=2,
        slow_period=5,
        signal_period=2,
        engine="polars",
    )


def macd_null_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.augment_macd(
        _pandas_group(frame),
        date_column="date",
        close_column="value",
        fast_period=2,
        slow_period=4,
        signal_period=2,
        engine="pandas",
    )


def macd_null_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.augment_macd(
        _polars_group(frame),
        date_column="date",
        close_column="value",
        fast_period=2,
        slow_period=4,
        signal_period=2,
        engine="polars",
    )


def pad_control_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.pad_by_time(frame, date_column="date", freq="1D", engine="pandas")


def pad_control_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.pad_by_time(frame, date_column="date", freq="1D", engine="polars")


def pad_grouped_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.pad_by_time(
        _pandas_group(frame),
        date_column="date",
        freq="1D",
        start_date="2024-01-01",
        end_date="2024-01-07",
        engine="pandas",
    )


def pad_grouped_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.pad_by_time(
        _polars_group(frame),
        date_column="date",
        freq="1D",
        start_date="2024-01-01",
        end_date="2024-01-07",
        engine="polars",
    )


def pad_null_pandas(frame: pd.DataFrame) -> pd.DataFrame:
    return tk.pad_by_time(
        _pandas_group(frame),
        date_column="date",
        freq="1D",
        fillna=0,
        engine="pandas",
    )


def pad_null_polars(frame: pl.DataFrame) -> pl.DataFrame:
    return tk.pad_by_time(
        _polars_group(frame),
        date_column="date",
        freq="1D",
        fillna=0,
        engine="polars",
    )
