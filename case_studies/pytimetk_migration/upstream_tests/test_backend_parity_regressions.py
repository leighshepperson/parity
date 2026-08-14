"""Focused pandas/Polars parity regressions for PyTimeTK 2.5.1.

Copy this file into the upstream test tree, or run it directly against an
editable checkout after applying ``patches/candidate.patch``.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
import polars as pl
import pytimetk as tk
from pandas.api.types import is_numeric_dtype


def _assert_semantic_frame_equal(
    reference: pd.DataFrame | pl.DataFrame,
    candidate: pd.DataFrame | pl.DataFrame,
    columns: Sequence[str],
) -> None:
    """Compare values and null masks while allowing backend dtype differences."""
    if isinstance(reference, pl.DataFrame):
        reference = reference.to_pandas()
    if isinstance(candidate, pl.DataFrame):
        candidate = candidate.to_pandas()
    reference = reference.reset_index(drop=True)
    candidate = candidate.reset_index(drop=True)
    assert len(reference) == len(candidate)

    for column in columns:
        assert column in reference.columns
        assert column in candidate.columns
        left = reference[column]
        right = candidate[column]
        left_null = pd.isna(left).to_numpy()
        right_null = pd.isna(right).to_numpy()
        np.testing.assert_array_equal(left_null, right_null, err_msg=column)

        left_values = left.loc[~left_null].reset_index(drop=True)
        right_values = right.loc[~right_null].reset_index(drop=True)
        if is_numeric_dtype(left_values.dtype) and is_numeric_dtype(right_values.dtype):
            np.testing.assert_allclose(
                left_values.to_numpy(dtype=float),
                right_values.to_numpy(dtype=float),
                rtol=1e-12,
                atol=1e-12,
                err_msg=column,
            )
        else:
            assert left_values.tolist() == right_values.tolist(), column


def _null_date_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-03", None, "2024-01-01", "2024-01-02"]),
            "value": [30.0, 99.0, 10.0, 20.0],
        }
    )


def _null_value_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5, freq="D"),
            "value": [1.0, 2.0, None, 4.0, 5.0],
        }
    )


def _unsorted_group_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": range(8),
            "group": ["A", "B", "A", "B", "A", "B", "A", "B"],
            "date": pd.to_datetime(
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
            "value": [3.0, 20.0, 1.0, 10.0, 2.0, 30.0, 4.0, 40.0],
        }
    )


def _hostile_group_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": range(10),
            "group": ["A", None, "A", "B", "A", None, "A", "B", "B", "B"],
            "date": pd.to_datetime(
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
            "value": [3.0, 20.0, 1.0, 10.0, None, 30.0, 4.0, 90.0, None, 30.0],
        }
    )


def test_augment_lags_places_null_dates_last_in_both_engines() -> None:
    data = _null_date_frame()
    kwargs = {"date_column": "date", "value_column": "value", "lags": 1}

    reference = tk.augment_lags(data=data.copy(), engine="pandas", **kwargs)
    candidate = tk.augment_lags(data=data.copy(), engine="polars", **kwargs)

    _assert_semantic_frame_equal(reference, candidate, ["date", "value_lag_1"])


def test_augment_lags_masks_nullable_group_results_like_pandas() -> None:
    data = _hostile_group_frame()
    kwargs = {"date_column": "date", "value_column": "value", "lags": 1}

    reference = tk.augment_lags(
        data=data.groupby("group", sort=False, dropna=False),
        engine="pandas",
        **kwargs,
    )
    candidate = tk.augment_lags(
        data=pl.from_pandas(data).group_by("group", maintain_order=True),
        engine="polars",
        **kwargs,
    )

    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)


def test_augment_rolling_places_null_dates_last_in_both_engines() -> None:
    data = _null_date_frame()
    kwargs = {
        "date_column": "date",
        "value_column": "value",
        "window": 2,
        "window_func": "sum",
        "min_periods": 1,
        "show_progress": False,
    }

    reference = tk.augment_rolling(data=data.copy(), engine="pandas", **kwargs)
    candidate = tk.augment_rolling(data=data.copy(), engine="polars", **kwargs)

    _assert_semantic_frame_equal(reference, candidate, ["date", "value_rolling_sum_win_2"])


def test_augment_rolling_preserves_pandas_nullable_group_placeholders() -> None:
    data = _hostile_group_frame()
    kwargs = {
        "date_column": "date",
        "value_column": "value",
        "window": 2,
        "window_func": "mean",
        "min_periods": 1,
        "show_progress": False,
    }

    reference = tk.augment_rolling(
        data=data.groupby("group", sort=False, dropna=False),
        engine="pandas",
        **kwargs,
    )
    candidate = tk.augment_rolling(
        data=pl.from_pandas(data).group_by("group", maintain_order=True),
        engine="polars",
        **kwargs,
    )

    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)


def test_augment_ewm_matches_null_date_order_and_unbiased_statistics() -> None:
    data = _null_date_frame()
    kwargs = {
        "date_column": "date",
        "value_column": "value",
        "window_func": ["mean", "std", "var"],
        "alpha": 0.5,
    }

    reference = tk.augment_ewm(data=data.copy(), engine="pandas", **kwargs)
    candidate = tk.augment_ewm(data=data.copy(), engine="polars", **kwargs)

    _assert_semantic_frame_equal(
        reference,
        candidate,
        [
            "date",
            "value_ewm_mean_alpha_0.5",
            "value_ewm_std_alpha_0.5",
            "value_ewm_var_alpha_0.5",
        ],
    )


def test_augment_ewm_preserves_pandas_null_propagation() -> None:
    data = _null_value_frame()
    kwargs = {
        "date_column": "date",
        "value_column": "value",
        "window_func": ["mean", "std", "var"],
        "alpha": 0.5,
    }

    reference = tk.augment_ewm(data=data.copy(), engine="pandas", **kwargs)
    candidate = tk.augment_ewm(data=data.copy(), engine="polars", **kwargs)

    _assert_semantic_frame_equal(
        reference,
        candidate,
        [
            "value_ewm_mean_alpha_0.5",
            "value_ewm_std_alpha_0.5",
            "value_ewm_var_alpha_0.5",
        ],
    )


def test_augment_ewm_restores_grouped_input_order() -> None:
    data = _unsorted_group_frame()
    kwargs = {
        "date_column": "date",
        "value_column": "value",
        "window_func": "mean",
        "alpha": 0.3,
        "adjust": False,
    }

    reference = tk.augment_ewm(
        data=data.groupby("group", sort=False, dropna=False),
        engine="pandas",
        **kwargs,
    )
    candidate = tk.augment_ewm(
        data=pl.from_pandas(data).group_by("group", maintain_order=True),
        engine="polars",
        **kwargs,
    )

    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)


def test_augment_ewm_fallback_preserves_group_and_row_identity() -> None:
    data = _hostile_group_frame()
    kwargs = {
        "date_column": "date",
        "value_column": "value",
        "window_func": ["mean", "std"],
        "alpha": 0.3,
        "adjust": False,
        "min_periods": 1,
    }

    reference = tk.augment_ewm(
        data=data.groupby("group", sort=False, dropna=False),
        engine="pandas",
        **kwargs,
    )
    candidate = tk.augment_ewm(
        data=pl.from_pandas(data).group_by("group", maintain_order=True),
        engine="polars",
        **kwargs,
    )

    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)


def test_augment_macd_places_null_dates_last_in_both_engines() -> None:
    data = _null_date_frame()
    kwargs = {
        "date_column": "date",
        "close_column": "value",
        "fast_period": 2,
        "slow_period": 3,
        "signal_period": 2,
    }

    reference = tk.augment_macd(data=data.copy(), engine="pandas", **kwargs)
    candidate = tk.augment_macd(data=data.copy(), engine="polars", **kwargs)

    _assert_semantic_frame_equal(
        reference,
        candidate,
        [
            "date",
            "value_macd_line_2_3_2",
            "value_macd_signal_line_2_3_2",
            "value_macd_histogram_2_3_2",
        ],
    )


def test_augment_macd_preserves_pandas_null_propagation() -> None:
    data = _null_value_frame()
    kwargs = {
        "date_column": "date",
        "close_column": "value",
        "fast_period": 2,
        "slow_period": 3,
        "signal_period": 2,
    }

    reference = tk.augment_macd(data=data.copy(), engine="pandas", **kwargs)
    candidate = tk.augment_macd(data=data.copy(), engine="polars", **kwargs)

    _assert_semantic_frame_equal(
        reference,
        candidate,
        [
            "value_macd_line_2_3_2",
            "value_macd_signal_line_2_3_2",
            "value_macd_histogram_2_3_2",
        ],
    )


def test_augment_macd_fallback_preserves_group_and_row_identity() -> None:
    data = _hostile_group_frame()
    kwargs = {
        "date_column": "date",
        "close_column": "value",
        "fast_period": 2,
        "slow_period": 4,
        "signal_period": 2,
    }

    reference = tk.augment_macd(
        data=data.groupby("group", sort=False, dropna=False),
        engine="pandas",
        **kwargs,
    )
    candidate = tk.augment_macd(
        data=pl.from_pandas(data).group_by("group", maintain_order=True),
        engine="polars",
        **kwargs,
    )

    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)


def test_pad_by_time_matches_legacy_nullable_group_exclusion() -> None:
    data = pd.DataFrame(
        {
            "group": pd.Series(["a", "a", pd.NA, pd.NA], dtype="string"),
            "date": pd.to_datetime(["2024-01-01", "2024-01-03", "2024-01-01", "2024-01-03"]),
            "value": [1.0, 3.0, 10.0, 30.0],
        }
    )
    kwargs = {"date_column": "date", "freq": "D"}

    reference = tk.pad_by_time(data=data.groupby("group", dropna=False), engine="pandas", **kwargs)
    candidate = tk.pad_by_time(
        data=pl.from_pandas(data).group_by("group", maintain_order=True),
        engine="polars",
        **kwargs,
    )

    assert len(reference) == 3
    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)


def test_pad_by_time_ungrouped_column_and_constant_semantics_match_pandas() -> None:
    data = pd.DataFrame(
        {
            "series": ["S", "S", "S"],
            "date": pd.to_datetime(["2024-01-01", "2024-01-03", "2024-01-06"]),
            "value": [1.0, 3.0, 6.0],
            "constant": ["x", "x", "x"],
        }
    )
    kwargs = {"date_column": "date", "freq": "D"}

    reference = tk.pad_by_time(data=data, engine="pandas", **kwargs)
    candidate = tk.pad_by_time(data=pl.from_pandas(data), engine="polars", **kwargs)

    assert reference.columns.tolist() == ["date", "series", "value", "constant"]
    assert reference.columns.tolist() == candidate.columns
    _assert_semantic_frame_equal(reference, candidate, reference.columns)
