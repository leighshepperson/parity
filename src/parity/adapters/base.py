"""Dataframe interchange adapters.

Arrow is Parity's internal wire format.  Adapters deliberately contain no
comparison logic: their only job is to move a rectangular value into and out
of that neutral representation without importing private implementation APIs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import pyarrow as pa


class AdapterError(TypeError):
    """Raised when a value cannot be represented by a requested adapter."""


class DataFrameAdapter(ABC):
    """Public adapter contract used by the runner and third-party adapters."""

    name: ClassVar[str]

    @abstractmethod
    def accepts(self, value: Any) -> bool:
        """Return whether *value* is a native value for this adapter."""

    @abstractmethod
    def to_arrow(self, value: Any) -> pa.Table:
        """Convert a native dataframe-like value to an Arrow table."""

    @abstractmethod
    def from_arrow(self, table: pa.Table) -> Any:
        """Convert an Arrow table to this adapter's native dataframe."""

    def load(self, path: Path) -> Any:
        """Load a fixture using the shared Arrow loader."""

        # Local import avoids a registry/import cycle.
        from parity.adapters.registry import load_arrow_fixture

        return self.from_arrow(load_arrow_fixture(path))


def ensure_table(value: pa.Table | pa.RecordBatch) -> pa.Table:
    """Return a table for either Arrow tabular container."""

    if isinstance(value, pa.Table):
        return value.combine_chunks()
    if isinstance(value, pa.RecordBatch):
        return pa.Table.from_batches([value])
    raise AdapterError(f"expected pyarrow.Table or RecordBatch, got {type(value).__name__}")
