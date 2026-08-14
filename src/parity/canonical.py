"""Canonical semantic values used by comparison and artifact rendering."""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa

from parity.adapters import detect_adapter
from parity.models import JsonValue


@dataclass(frozen=True, slots=True)
class CanonicalColumn:
    """One named, typed column in a canonical frame."""

    name: str
    dtype: str
    family: str
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class CanonicalFrame:
    """A library-neutral dataframe value."""

    columns: tuple[CanonicalColumn, ...]

    @property
    def height(self) -> int:
        return len(self.columns[0].values) if self.columns else 0

    @property
    def width(self) -> int:
        return len(self.columns)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def rows(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            tuple(column.values[index] for column in self.columns) for index in range(self.height)
        )


@dataclass(frozen=True, slots=True)
class CanonicalSeries:
    """A named one-dimensional result, distinct from a one-column frame."""

    name: str | None
    dtype: str
    family: str
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ExceptionInfo:
    """Stable public description of an exception result."""

    type_name: str
    message: str
    module: str | None = None

    @classmethod
    def from_exception(cls, error: BaseException) -> ExceptionInfo:
        error_type = type(error)
        return cls(error_type.__qualname__, str(error), error_type.__module__)


def dtype_family(dtype: Any) -> str:
    """Map Arrow, pandas, Polars, NumPy, or textual dtypes to portable families."""

    if isinstance(dtype, pa.DataType):
        if pa.types.is_dictionary(dtype):
            return "category"
        if pa.types.is_boolean(dtype):
            return "boolean"
        if pa.types.is_integer(dtype):
            return "integer"
        if pa.types.is_floating(dtype):
            return "float"
        if pa.types.is_decimal(dtype):
            return "decimal"
        if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
            return "string"
        if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype):
            return "binary"
        if pa.types.is_date(dtype):
            return "date"
        if pa.types.is_timestamp(dtype):
            return "datetime"
        if pa.types.is_time(dtype):
            return "time"
        if pa.types.is_duration(dtype):
            return "duration"
        if (
            pa.types.is_list(dtype)
            or pa.types.is_large_list(dtype)
            or pa.types.is_fixed_size_list(dtype)
        ):
            return "list"
        if pa.types.is_struct(dtype):
            return "struct"
        if pa.types.is_map(dtype):
            return "map"
        if pa.types.is_null(dtype):
            return "null"
        return "object"

    text = str(dtype).strip().lower()
    if any(token in text for token in ("category", "categorical", "dictionary", "enum")):
        return "category"
    if text in {"bool", "boolean"}:
        return "boolean"
    if any(token in text for token in ("uint", "int")) and "interval" not in text:
        return "integer"
    if any(token in text for token in ("float", "double", "real")):
        return "float"
    if any(token in text for token in ("decimal", "numeric")):
        return "decimal"
    if any(token in text for token in ("datetime", "timestamp")):
        return "datetime"
    if text.startswith("date"):
        return "date"
    if text.startswith("time"):
        return "time"
    if any(token in text for token in ("duration", "timedelta", "interval")):
        return "duration"
    if any(token in text for token in ("utf8", "string", "str", "varchar", "text")):
        return "string"
    if any(token in text for token in ("binary", "bytes")):
        return "binary"
    if any(token in text for token in ("list", "array")):
        return "list"
    if "struct" in text:
        return "struct"
    if "map" in text or "dict" in text:
        return "map"
    if text in {"null", "none", "void"}:
        return "null"
    return "object"


def _canonical_frame(value: Any) -> CanonicalFrame:
    table = detect_adapter(value).to_arrow(value)
    columns = tuple(
        CanonicalColumn(
            name=field.name,
            dtype=str(field.type),
            family=dtype_family(field.type),
            values=tuple(normalize_scalar(item) for item in table.column(index).to_pylist()),
        )
        for index, field in enumerate(table.schema)
    )
    return CanonicalFrame(columns)


def _canonical_series(value: pd.Series | pl.Series) -> CanonicalSeries:
    if isinstance(value, pd.Series):
        arrow = pa.array(value)
        name = None if value.name is None else str(value.name)
    else:
        arrow = value.to_arrow()
        name = value.name or None
    return CanonicalSeries(
        name=name,
        dtype=str(arrow.type),
        family=dtype_family(arrow.type),
        values=tuple(normalize_scalar(item) for item in arrow.to_pylist()),
    )


def canonicalize(value: Any) -> Any:
    """Convert supported outputs to library-neutral Python structures."""

    # Canonical values can re-enter recursively through mappings and sequences.
    # Treat them as fixed points instead of expanding their dataclass fields.
    if isinstance(value, (CanonicalFrame, CanonicalSeries, ExceptionInfo)):
        return value
    if isinstance(value, BaseException):
        return ExceptionInfo.from_exception(value)
    if isinstance(value, (pd.Series, pl.Series)):
        return _canonical_series(value)
    try:
        detect_adapter(value)
    except TypeError:
        pass
    else:
        return _canonical_frame(value)
    if isinstance(value, pa.Array | pa.ChunkedArray):
        return tuple(normalize_scalar(item) for item in value.to_pylist())
    if isinstance(value, np.ndarray):
        return canonicalize(value.tolist())
    if isinstance(value, Mapping):
        return {normalize_scalar(key): canonicalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return frozenset(canonicalize(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(canonicalize(item) for item in value)
    return normalize_scalar(value)


def normalize_scalar(value: Any) -> Any:
    """Unbox third-party scalar containers while preserving semantic values."""

    if isinstance(value, pa.Scalar):
        return normalize_scalar(value.as_py())
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        unboxed = value.item()
        # Wider-than-binary64 NumPy floats deliberately remain NumPy scalars:
        # ``longdouble.item()`` can return another ``longdouble`` and recursive
        # unboxing would never terminate. The comparator can consume its exact
        # integer ratio without narrowing it to a Python float.
        return value if isinstance(unboxed, np.generic) else normalize_scalar(unboxed)
    if isinstance(value, enum.Enum):
        return normalize_scalar(value.value)
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return canonicalize(dataclasses.asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return canonicalize(value.model_dump(mode="python"))
    return value


def is_null(value: Any) -> bool:
    """Return whether a scalar is a database-style null (not IEEE NaN)."""

    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, (dt.datetime, dt.date, dt.time, dt.timedelta)):
        return False
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, (bool, np.bool_)) and not is_nan(value) else False


def is_nan(value: Any) -> bool:
    """Return whether a value is an IEEE NaN."""

    if isinstance(value, decimal.Decimal):
        return value.is_nan()
    if isinstance(value, np.floating):
        return bool(np.isnan(value))
    return isinstance(value, float) and math.isnan(value)


def json_safe(value: Any, *, max_items: int = 20) -> JsonValue:
    """Render arbitrary comparison values into the result model's JSON domain."""

    value = normalize_scalar(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0 and math.copysign(1.0, value) < 0:
            return "-0.0"
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        return f"0x{value.hex()}"
    if isinstance(value, CanonicalFrame):
        return {
            "columns": [column.name for column in value.columns],
            "dtypes": [column.dtype for column in value.columns],
            "rows": [json_safe(row) for row in value.rows()[:max_items]],
        }
    if isinstance(value, CanonicalSeries):
        return {
            "name": value.name,
            "dtype": value.dtype,
            "values": [json_safe(item) for item in value.values[:max_items]],
        }
    if isinstance(value, ExceptionInfo):
        return {"type": value.type_name, "message": value.message, "module": value.module}
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item, max_items=max_items)
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (Sequence, set, frozenset)) and not isinstance(value, (str, bytes)):
        items = list(value)
        return [json_safe(item, max_items=max_items) for item in items[:max_items]]
    return repr(value)


__all__ = [
    "CanonicalColumn",
    "CanonicalFrame",
    "CanonicalSeries",
    "ExceptionInfo",
    "canonicalize",
    "dtype_family",
    "is_nan",
    "is_null",
    "json_safe",
    "normalize_scalar",
]
