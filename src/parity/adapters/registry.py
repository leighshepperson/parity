"""Adapter registry and fixture loading."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.feather as pa_feather
import pyarrow.ipc as pa_ipc
import pyarrow.json as pa_json
import pyarrow.parquet as pa_parquet

from parity.adapters.arrow import ArrowAdapter
from parity.adapters.base import AdapterError, DataFrameAdapter
from parity.adapters.pandas import PandasAdapter
from parity.adapters.polars import PolarsAdapter

_REGISTRY: dict[str, DataFrameAdapter] = {}


def register_adapter(adapter: DataFrameAdapter, *, replace: bool = False) -> None:
    """Register an adapter by its lower-case name.

    Third-party adapters can call this during application startup.  Accidental
    replacement is rejected so plugin ordering cannot silently change results.
    """

    name = adapter.name.lower()
    if not name or name == "auto":
        raise ValueError("adapter name must be non-empty and cannot be 'auto'")
    if name in _REGISTRY and not replace:
        raise ValueError(f"adapter already registered: {name}")
    _REGISTRY[name] = adapter


def available_adapters() -> tuple[str, ...]:
    """Return registered adapter names in stable order."""

    return tuple(sorted(_REGISTRY))


def detect_adapter(value: Any) -> DataFrameAdapter:
    """Find the registered adapter for a native value."""

    for name in ("arrow", "pandas", "polars"):
        adapter = _REGISTRY.get(name)
        if adapter is not None and adapter.accepts(value):
            return adapter
    for name, adapter in _REGISTRY.items():
        if name not in {"arrow", "pandas", "polars"} and adapter.accepts(value):
            return adapter
    supported = ", ".join(available_adapters())
    raise AdapterError(f"no adapter for {type(value).__name__}; available adapters: {supported}")


def get_adapter(
    adapter: str | DataFrameAdapter | Any = "auto", *, value: Any = None
) -> DataFrameAdapter:
    """Resolve an adapter name, instance, or native value.

    ``get_adapter("auto", value=frame)`` and ``get_adapter(frame)`` are both
    supported for convenient use from Python and from the execution engine.
    """

    if isinstance(adapter, DataFrameAdapter):
        return adapter
    if not isinstance(adapter, str):
        return detect_adapter(adapter)
    name = adapter.lower()
    if name == "auto":
        if value is None:
            raise AdapterError("automatic adapter selection requires a value")
        return detect_adapter(value)
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise AdapterError(
            f"unknown adapter {adapter!r}; choose one of {', '.join(available_adapters())}"
        ) from exc


def to_arrow(value: Any, adapter: str | DataFrameAdapter = "auto") -> pa.Table:
    """Convert a registered dataframe value to canonical Arrow."""

    return get_adapter(adapter, value=value).to_arrow(value)


def from_arrow(table: pa.Table, adapter: str | DataFrameAdapter = "arrow") -> Any:
    """Convert canonical Arrow into a requested dataframe implementation."""

    return get_adapter(adapter).from_arrow(table)


def _read_json_array(path: Path) -> pa.Table:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        # Support both a single row and conventional {"rows": [...]} fixtures.
        rows = raw.get("rows")
        raw = rows if isinstance(rows, list) else [raw]
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise ValueError("JSON fixture must be an object, an array of objects, or {'rows': [...]}")
    return pa.Table.from_pylist(raw)


def _read_arrow(path: Path) -> pa.Table:
    source = pa.memory_map(str(path), "r")
    try:
        return pa_ipc.open_file(source).read_all()
    except pa.ArrowInvalid:
        source.seek(0)
        return pa_ipc.open_stream(source).read_all()


def load_arrow_fixture(path: str | Path) -> pa.Table:
    """Load CSV, JSON/JSONL, Parquet, Feather, or Arrow IPC as a table."""

    fixture = Path(path)
    if not fixture.is_file():
        raise FileNotFoundError(f"fixture not found: {fixture}")
    suffix = fixture.suffix.lower()
    if suffix == ".csv":
        table = pa_csv.read_csv(fixture)
    elif suffix in {".jsonl", ".ndjson"}:
        table = pa_json.read_json(fixture)
    elif suffix == ".json":
        # Arrow's reader consumes newline-delimited JSON; ordinary JSON arrays
        # are common in hand-authored fixtures and need a tiny public-API path.
        table = _read_json_array(fixture)
    elif suffix in {".parquet", ".pq"}:
        table = pa_parquet.read_table(fixture)
    elif suffix in {".feather"}:
        table = pa_feather.read_table(fixture)
    elif suffix in {".arrow", ".ipc"}:
        table = _read_arrow(fixture)
    else:
        supported = ".csv, .json, .jsonl, .ndjson, .parquet, .pq, .feather, .arrow, .ipc"
        raise ValueError(f"unsupported fixture extension {suffix!r}; supported: {supported}")
    return table.combine_chunks()


def load_fixture(path: str | Path, adapter: str | DataFrameAdapter = "arrow") -> Any:
    """Load a fixture and return it in the requested dataframe implementation."""

    return from_arrow(load_arrow_fixture(path), adapter)


def convert_many(values: Iterable[Any], adapter: str | DataFrameAdapter) -> list[Any]:
    """Convert several native frames through Arrow into one implementation."""

    return [from_arrow(to_arrow(value), adapter) for value in values]


for _adapter in (ArrowAdapter(), PandasAdapter(), PolarsAdapter()):
    register_adapter(_adapter)
