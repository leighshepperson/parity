"""Corrected candidates for the synthetic fault corpus."""

from __future__ import annotations

import polars as pl


def join_fixed(frame: pl.DataFrame) -> pl.DataFrame:
    """Normalise null join keys explicitly for cross-version Polars behaviour."""

    sentinel = -(2**63)
    lookup = (
        pl.DataFrame({"customer_id": [1, None], "segment": ["known", "unknown"]})
        .with_columns(pl.col("customer_id").fill_null(sentinel).alias("_join_key"))
        .select("_join_key", "segment")
    )
    return (
        frame.with_columns(pl.col("customer_id").fill_null(sentinel).alias("_join_key"))
        .join(lookup, on="_join_key", how="left")
        .with_columns(pl.col("customer_id").cast(pl.String).fill_null("<missing>"))
        .select("customer_id", "segment")
    )


def groupby_fixed(frame: pl.DataFrame) -> pl.DataFrame:
    return (
        frame.with_columns(pl.col("region").fill_null("<missing>"))
        .group_by("region")
        .agg(
            pl.when(pl.col("amount").drop_nulls().drop_nans().len() == 0)
            .then(pl.when(pl.col("amount").is_null().any()).then(None).otherwise(float("nan")))
            .otherwise(pl.col("amount").drop_nans().sum())
            .alias("amount")
        )
    )


def timezone_fixed(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.select(
        pl.col("event_at")
        .str.to_datetime(time_zone="UTC", strict=False)
        .dt.convert_time_zone("America/New_York")
        .dt.strftime("%Y-%m-%d")
        .alias("local_day")
    )


def dtype_fixed(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.select(pl.col("quantity").cast(pl.Int64))


def ordering_fixed(frame: pl.DataFrame) -> pl.DataFrame:
    return (
        frame.with_row_index("_arrival")
        .sort(["priority", "_arrival"], nulls_last=True)
        .select("record_id", "priority")
    )
