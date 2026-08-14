"""Deliberately incorrect pandas-to-Polars migrations using synthetic domains.

Each function is small enough to audit. The mistakes are representative of public,
documented engine differences; none derives from private application code or data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl


def join_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """The existing contract matches missing customer keys."""

    lookup = pd.DataFrame(
        {"customer_id": pd.Series([1, None], dtype="Int64"), "segment": ["known", "unknown"]}
    )
    result = frame.merge(lookup, on="customer_id", how="left")[["customer_id", "segment"]]
    result["customer_id"] = (
        result["customer_id"]
        .map(lambda value: "<missing>" if pd.isna(value) else str(int(value)))
        .astype("string")
    )
    return result


def join_bad(frame: pl.DataFrame) -> pl.DataFrame:
    """Bug: a default Polars join does not match null keys."""

    lookup = pl.DataFrame({"customer_id": [1, None], "segment": ["known", "unknown"]})
    return frame.join(lookup, on="customer_id", how="left").select("customer_id", "segment")


def groupby_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """The existing contract retains the missing-region bucket."""

    frame = frame.copy()
    frame["region"] = frame["region"].fillna("<missing>")
    rows: list[dict[str, object]] = []
    for region, group in frame.groupby("region", dropna=False, sort=True):
        values = group["amount"].tolist()
        present = [value for value in values if not pd.isna(value)]
        if present:
            amount: object = float(sum(present))
        elif any(value is None or value is pd.NA for value in values):
            amount = None
        else:
            amount = float("nan")
        rows.append({"region": region, "amount": amount})
    if not rows:
        return pd.DataFrame(
            {
                "region": pd.Series([], dtype="string"),
                "amount": pd.Series([], dtype="float64"),
            }
        )
    result = pd.DataFrame(rows, columns=["region", "amount"])
    result["amount"] = pd.Series([row["amount"] for row in rows], dtype=object)
    return result


def groupby_bad(frame: pl.DataFrame) -> pl.DataFrame:
    """Bug: an over-eager cleanup silently deletes the missing-region bucket."""

    return frame.drop_nulls("region").group_by("region").agg(pl.col("amount").sum())


def timezone_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """Business days are defined in New York, not UTC."""

    timestamps = pd.to_datetime(frame["event_at"], utc=True)
    return pd.DataFrame(
        {"local_day": timestamps.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")}
    )


def timezone_bad(frame: pl.DataFrame) -> pl.DataFrame:
    """Bug: formatting the UTC date first moves observations across day boundaries."""

    return frame.select(
        pl.col("event_at")
        .str.to_datetime(time_zone="UTC", strict=False)
        .dt.strftime("%Y-%m-%d")
        .alias("local_day")
    )


def dtype_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """The public output contract is nullable 64-bit integer."""

    return pd.DataFrame({"quantity": pd.to_numeric(frame["quantity"]).astype("Int64")})


def dtype_bad(frame: pl.DataFrame) -> pl.DataFrame:
    """Bug: a narrower cast converts out-of-range values to null."""

    return frame.select(pl.col("quantity").cast(pl.Int8, strict=False))


def ordering_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """Priority is sorted while equal-priority records retain arrival order."""

    return frame.sort_values("priority", kind="stable", ignore_index=True)[
        ["record_id", "priority"]
    ]


def ordering_bad(frame: pl.DataFrame) -> pl.DataFrame:
    """Bug: adding a tie-breaker changes the stable arrival order."""

    return frame.sort(["priority", "record_id"]).select("record_id", "priority")


def integer_precision_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """Integer identifiers retain every bit across the transformation."""

    return frame[["quantity"]].copy()


def integer_precision_bad(frame: pl.DataFrame) -> pl.DataFrame:
    """Bug: a binary-float round trip loses integers above 2**53."""

    return frame.select(pl.col("quantity").cast(pl.Float64).cast(pl.Int64))


def make_demo_inputs() -> dict[str, pd.DataFrame]:
    """Return deterministic witnesses used in documentation and integrity tests."""

    return {
        "null-join": pd.DataFrame({"customer_id": pd.Series([None, 1], dtype="Int64")}),
        "groupby-null": pd.DataFrame({"region": [None, "north"], "amount": [5.0, 7.0]}),
        "timezone-day": pd.DataFrame({"event_at": ["2026-01-01T01:30:00Z"]}),
        "dtype-width": pd.DataFrame({"quantity": pd.Series([128], dtype="Int64")}),
        "stable-order": pd.DataFrame(
            {"record_id": [20, 10, 30], "priority": np.array([1, 1, 0], dtype=np.int64)}
        ),
        "integer-precision": pd.DataFrame({"quantity": pd.Series([2**53 + 1], dtype="int64")}),
    }
