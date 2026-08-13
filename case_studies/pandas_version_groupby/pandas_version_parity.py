"""One unchanged pandas computation executed by two worker runtimes."""

from __future__ import annotations

import pandas as pd


def categorical_total(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a fixed categorical domain while omitting the observed option."""

    prepared = frame.copy(deep=True)
    prepared["segment"] = pd.Categorical(
        prepared["segment"],
        categories=["used", "unused"],
    )
    return prepared.groupby("segment", as_index=False)["value"].sum()
