"""One unchanged Polars computation executed by two worker runtimes."""

from __future__ import annotations

import polars as pl


def dynamic_window(frame: pl.DataFrame) -> pl.DataFrame:
    """Group boundary observations while intentionally leaving offset at its default."""

    parsed = frame.with_columns(pl.col("ts").str.strptime(pl.Datetime))
    return (
        parsed.group_by_dynamic(
            "ts",
            every="1h",
            closed="both",
            label="left",
        )
        .agg(pl.col("value").sum())
        .sort("ts")
    )
