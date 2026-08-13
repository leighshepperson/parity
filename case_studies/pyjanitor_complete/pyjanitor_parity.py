"""Cross-backend Parity wrappers around pyjanitor's upstream ``complete`` APIs."""

from __future__ import annotations

import janitor  # Register pandas dataframe methods.
import janitor.polars  # noqa: F401  # Register Polars dataframe methods.
import pandas as pd
import polars as pl


def pandas_grid(frame: pd.DataFrame) -> pd.DataFrame:
    """Complete a two-dimensional grid, retaining backend-native semantics."""

    return frame.complete("store", "sku", sort=True)


def polars_grid(frame: pl.DataFrame) -> pl.DataFrame:
    """Complete the same two-dimensional grid through pyjanitor's Polars API."""

    return frame.complete("store", "sku", sort=True)


def pandas_fill_implicit(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill only rows introduced by completion."""

    return frame.complete(
        "store",
        "sku",
        fill_value={"metric": 0.0},
        explicit=False,
        sort=True,
    )


def polars_fill_implicit(frame: pl.DataFrame) -> pl.DataFrame:
    """Fill only rows introduced by completion."""

    return frame.complete(
        "store",
        "sku",
        fill_value={"metric": 0.0},
        explicit=False,
        sort=True,
    )


def pandas_fill_explicit(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill both existing and introduced null payloads."""

    return frame.complete(
        "store",
        "sku",
        fill_value={"metric": 0.0},
        explicit=True,
        sort=True,
    )


def polars_fill_explicit(frame: pl.DataFrame) -> pl.DataFrame:
    """Fill both existing and introduced null payloads."""

    return frame.complete(
        "store",
        "sku",
        fill_value={"metric": 0.0},
        explicit=True,
        sort=True,
    )


def pandas_grouped(frame: pd.DataFrame) -> pd.DataFrame:
    """Complete week-by-SKU combinations independently per warehouse."""

    return frame.complete("week", "sku", by="warehouse", sort=True)


def polars_grouped(frame: pl.DataFrame) -> pl.DataFrame:
    """Complete week-by-SKU combinations independently per warehouse."""

    return frame.complete("week", "sku", by="warehouse", sort=True)


def pandas_nested_pair(frame: pd.DataFrame) -> pd.DataFrame:
    """Cross groups with observed ``(item_id, item_name)`` pairs."""

    return frame.complete("group", ["item_id", "item_name"], sort=True)


def polars_nested_pair(frame: pl.DataFrame) -> pl.DataFrame:
    """Express the same observed pair through Polars' struct API."""

    return (
        frame.select("group", pl.struct("item_id", "item_name"), "metric")
        .complete("group", "item_id", sort=True)
        .unnest("item_id")
    )


def pandas_unsorted(frame: pd.DataFrame) -> pd.DataFrame:
    """Complete using backend-native first-seen ordering."""

    return frame.complete("store", "sku", sort=False)


def polars_unsorted(frame: pl.DataFrame) -> pl.DataFrame:
    """Complete using backend-native unsorted ordering."""

    return frame.complete("store", "sku", sort=False)


def pandas_narrow_domain(frame: pd.DataFrame) -> pd.DataFrame:
    """Complete a supplied year domain while preserving original rows."""

    return frame.complete({"year": [2020, 2021]}, "product", sort=True)


def polars_narrow_domain(frame: pl.DataFrame) -> pl.DataFrame:
    """Complete the equivalent supplied year domain in Polars."""

    return frame.complete(pl.Series("year", [2020, 2021]), "product", sort=True)


def pandas_partial_fill(frame: pd.DataFrame) -> pd.DataFrame:
    """Request a fill for one of two nullable payload columns."""

    return frame.complete(
        "group",
        "item",
        fill_value={"qty": 0.0},
        explicit=True,
        sort=True,
    )


def polars_partial_fill(frame: pl.DataFrame) -> pl.DataFrame:
    """Request the equivalent single-column fill in Polars."""

    return frame.complete(
        "group",
        "item",
        fill_value={"qty": 0.0},
        explicit=True,
        sort=True,
    )


def pandas_index_collision(frame: pd.DataFrame) -> pd.DataFrame:
    """Exercise a legitimate user column named like Polars' internal marker."""

    return frame.complete("group", "item", fill_value=0, explicit=False, sort=True)


def polars_index_collision(frame: pl.DataFrame) -> pl.DataFrame:
    """Exercise the same public input through Polars."""

    return frame.complete("group", "item", fill_value=0, explicit=False, sort=True)
