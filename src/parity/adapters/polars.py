"""Polars dataframe and series adapter."""

from __future__ import annotations

from typing import Any

import polars as pl
import pyarrow as pa

from parity.adapters.base import AdapterError, DataFrameAdapter


class PolarsAdapter(DataFrameAdapter):
    name = "polars"

    def accepts(self, value: Any) -> bool:
        return isinstance(value, (pl.DataFrame, pl.LazyFrame, pl.Series))

    def to_arrow(self, value: Any) -> pa.Table:
        if isinstance(value, pl.LazyFrame):
            value = value.collect()
        if isinstance(value, pl.Series):
            value = value.to_frame()
        if not isinstance(value, pl.DataFrame):
            raise AdapterError(f"polars adapter cannot handle {type(value).__name__}")
        return value.to_arrow().combine_chunks()

    def from_arrow(self, table: pa.Table) -> pl.DataFrame:
        converted = pl.from_arrow(table, rechunk=True)
        # Polars' type hints cover both Arrow table and array inputs even though
        # this adapter accepts a table exclusively.
        return converted.to_frame() if isinstance(converted, pl.Series) else converted
