"""Direct two-frame compatibility targets for the input-bundle study."""

from __future__ import annotations

import pandas as pd
import polars as pl


def pandas_left_join(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    """Use pandas' documented default null-key merge semantics."""

    result = left.merge(right, on="key", how="left", sort=False)
    return result.sort_values("key", na_position="last", kind="stable").reset_index(drop=True)


def polars_left_join(left: pl.DataFrame, right: pl.DataFrame) -> pl.DataFrame:
    """Use Polars' documented default ``nulls_equal=False`` semantics."""

    return (
        # Polars 1.0 left joins already preserve the left-frame order. Avoid
        # ``maintain_order`` here because it was added to ``DataFrame.join``
        # after Parity's supported Polars floor.
        left.join(right, on="key", how="left")
        .select("key", "left_value", "right_value")
        .sort("key", nulls_last=True, maintain_order=True)
    )
