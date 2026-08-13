"""Apache Arrow adapter."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from parity.adapters.base import AdapterError, DataFrameAdapter, ensure_table


class ArrowAdapter(DataFrameAdapter):
    name = "arrow"

    def accepts(self, value: Any) -> bool:
        return isinstance(value, (pa.Table, pa.RecordBatch))

    def to_arrow(self, value: Any) -> pa.Table:
        if not self.accepts(value):
            raise AdapterError(f"arrow adapter cannot handle {type(value).__name__}")
        return ensure_table(value)

    def from_arrow(self, table: pa.Table) -> pa.Table:
        return ensure_table(table)
