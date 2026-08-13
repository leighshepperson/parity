"""Pandas adapter implemented through pandas' public Arrow conversion API."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd
import pyarrow as pa

from parity.adapters.base import AdapterError, DataFrameAdapter


class PandasAdapter(DataFrameAdapter):
    name = "pandas"

    def accepts(self, value: Any) -> bool:
        return isinstance(value, (pd.DataFrame, pd.Series))

    def to_arrow(self, value: Any) -> pa.Table:
        if isinstance(value, pd.Series):
            name = value.name if value.name is not None else "value"
            value = value.to_frame(name=name)
        if not isinstance(value, pd.DataFrame):
            raise AdapterError(f"pandas adapter cannot handle {type(value).__name__}")
        # Some pandas reductions legitimately place an ExtensionArray in a
        # single object cell (for example ``Series.mode`` when several values
        # tie).  Arrow understands the corresponding Python list, but cannot
        # infer a nested type directly from pandas' ExtensionArray scalar.
        # Normalize only those nested scalars; ordinary extension-typed
        # columns still use pandas' public Arrow conversion unchanged.
        nested_columns: list[tuple[int, list[Any]]] = []
        for position in range(value.shape[1]):
            source = value.iloc[:, position]
            if source.dtype != object:
                continue
            normalized: list[Any] = []
            changed = False
            for item in source.tolist():
                if isinstance(item, pd.api.extensions.ExtensionArray):
                    normalized.append(item.tolist())
                    changed = True
                else:
                    normalized.append(item)
            if changed:
                nested_columns.append((position, normalized))
        if nested_columns:
            value = value.copy()
            for position, normalized in nested_columns:
                value.isetitem(
                    position,
                    pd.Series(normalized, index=value.index, dtype=object),
                )
        # Indexes are intentionally not implicit data in a cross-engine check.
        table = pa.Table.from_pandas(value, preserve_index=False).combine_chunks()
        # Arrow's pandas conversion normally treats IEEE NaN as a null marker.
        # Parity must preserve that distinction for NumPy floating columns.
        # Pandas nullable floating arrays use pd.NA for missing values, so those
        # remain database nulls rather than becoming IEEE NaNs.
        for index, field in enumerate(table.schema):
            source = value.iloc[:, index]
            source_values = source.tolist()
            floating_numpy_dtype = isinstance(source.dtype, np.dtype) and np.issubdtype(
                source.dtype, np.floating
            )
            contains_float = any(isinstance(item, (float, np.floating)) for item in source_values)
            # An object column containing strings and NaN is legitimately
            # represented by Arrow as string + null; Arrow has no dense union
            # conversion for this pandas shape. Only recover NaN when the
            # inferred domain is floating, or all values were missing and
            # pandas therefore inferred Arrow's null type.
            if not floating_numpy_dtype and not (
                source.dtype == object
                and contains_float
                and (pa.types.is_floating(field.type) or pa.types.is_null(field.type))
            ):
                continue
            values = [None if item is pd.NA or item is pd.NaT else item for item in source_values]
            target_type = field.type if pa.types.is_floating(field.type) else None
            array = pa.array(values, type=target_type, from_pandas=False)
            table = table.set_column(index, field.with_type(array.type), array)
        return table

    def from_arrow(self, table: pa.Table) -> pd.DataFrame:
        # Arrow-backed pandas dtypes preserve the canonical distinction between
        # null, IEEE NaN, and nullable integers. The default NumPy conversion
        # coerces an integer null to float NaN before user code even runs.
        return cast(pd.DataFrame, table.to_pandas(types_mapper=pd.ArrowDtype))
