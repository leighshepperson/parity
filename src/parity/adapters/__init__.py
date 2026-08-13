"""Public dataframe adapter registry."""

from parity.adapters.arrow import ArrowAdapter
from parity.adapters.base import AdapterError, DataFrameAdapter
from parity.adapters.pandas import PandasAdapter
from parity.adapters.polars import PolarsAdapter
from parity.adapters.registry import (
    available_adapters,
    detect_adapter,
    from_arrow,
    get_adapter,
    load_arrow_fixture,
    load_fixture,
    register_adapter,
    to_arrow,
)

__all__ = [
    "AdapterError",
    "ArrowAdapter",
    "DataFrameAdapter",
    "PandasAdapter",
    "PolarsAdapter",
    "available_adapters",
    "detect_adapter",
    "from_arrow",
    "get_adapter",
    "load_arrow_fixture",
    "load_fixture",
    "register_adapter",
    "to_arrow",
]
