from __future__ import annotations

import json

import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
import pytest

from parity.adapters import (
    AdapterError,
    DataFrameAdapter,
    available_adapters,
    detect_adapter,
    get_adapter,
    load_fixture,
    register_adapter,
    to_arrow,
)


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (pd.DataFrame({"x": [1]}), "pandas"),
        (pl.DataFrame({"x": [1]}), "polars"),
        (pa.table({"x": [1]}), "arrow"),
    ],
)
def test_detect_and_roundtrip_registered_frames(value: object, name: str) -> None:
    adapter = detect_adapter(value)
    assert adapter.name == name
    table = adapter.to_arrow(value)
    assert table.to_pylist() == [{"x": 1}]
    assert adapter.accepts(adapter.from_arrow(table))


def test_series_are_one_column_tables_without_index() -> None:
    value = pd.Series([4, 5], name="score", index=[20, 30])
    table = to_arrow(value)
    assert table.column_names == ["score"]
    assert table.to_pydict() == {"score": [4, 5]}


def test_pandas_adapter_preserves_nan_distinct_from_nullable_null() -> None:
    nan_table = to_arrow(pd.DataFrame({"x": [1.0, float("nan")]}))
    assert nan_table.column("x").null_count == 0
    assert pd.isna(nan_table.column("x").to_pylist()[1])

    null_table = to_arrow(pd.DataFrame({"x": pd.Series([1.0, pd.NA], dtype="Float64")}))
    assert null_table.column("x").null_count == 1

    object_table = to_arrow(pd.DataFrame({"x": pd.Series([float("nan"), None], dtype=object)}))
    assert object_table.column("x").null_count == 1
    assert pd.isna(object_table.column("x").to_pylist()[0])

    mixed_table = to_arrow(pd.DataFrame({"x": pd.Series(["present", float("nan")], dtype=object)}))
    assert mixed_table.column("x").to_pylist() == ["present", None]


def test_registry_rejects_unknown_and_duplicate() -> None:
    assert available_adapters() == ("arrow", "pandas", "polars")
    with pytest.raises(AdapterError, match="unknown adapter"):
        get_adapter("spark")
    with pytest.raises(AdapterError, match="no adapter"):
        detect_adapter(object())
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(get_adapter("arrow"))


@pytest.mark.parametrize("extension", ["csv", "json", "jsonl", "parquet", "arrow"])
def test_load_fixture_formats(tmp_path, extension: str) -> None:
    path = tmp_path / f"input.{extension}"
    if extension == "csv":
        path.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    elif extension == "json":
        path.write_text(json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]), encoding="utf-8")
    elif extension == "jsonl":
        path.write_text('{"a": 1, "b": "x"}\n{"a": 2, "b": "y"}\n', encoding="utf-8")
    elif extension == "parquet":
        pq.write_table(pa.table({"a": [1, 2], "b": ["x", "y"]}), path)
    else:
        with (
            path.open("wb") as stream,
            ipc.new_file(stream, pa.schema([("a", pa.int64()), ("b", pa.string())])) as writer,
        ):
            writer.write_table(pa.table({"a": [1, 2], "b": ["x", "y"]}))

    table = load_fixture(path)
    assert table.to_pylist() == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert isinstance(load_fixture(path, "pandas"), pd.DataFrame)
    assert isinstance(load_fixture(path, "polars"), pl.DataFrame)


def test_load_fixture_errors_are_actionable(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="fixture not found"):
        load_fixture(tmp_path / "missing.csv")
    path = tmp_path / "input.xlsx"
    path.write_bytes(b"not an xlsx")
    with pytest.raises(ValueError, match="unsupported fixture extension"):
        load_fixture(path)


def test_adapter_extension_point() -> None:
    class Box:
        def __init__(self, table: pa.Table) -> None:
            self.table = table

    class BoxAdapter(DataFrameAdapter):
        name = "box-for-test"

        def accepts(self, value: object) -> bool:
            return isinstance(value, Box)

        def to_arrow(self, value: object) -> pa.Table:
            assert isinstance(value, Box)
            return value.table

        def from_arrow(self, table: pa.Table) -> Box:
            return Box(table)

    register_adapter(BoxAdapter(), replace=True)
    assert detect_adapter(Box(pa.table({"x": [1]}))).name == "box-for-test"
