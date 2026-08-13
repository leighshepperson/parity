from __future__ import annotations

import datetime as dt

import pandas as pd
import pyarrow as pa
import pytest

from parity.models import ColumnSchema, FrameSchema
from parity.schema import arrow_schema, infer_schema, table_from_rows


def test_infer_portable_schema_and_observed_constraints() -> None:
    frame = pd.DataFrame(
        {
            "id": pd.Series([1, 2, 3], dtype="int64"),
            "amount": [1.5, None, 4.0],
            "label": pd.Series(["a", "b", "a"], dtype="category"),
            "when": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
        }
    )
    schema = infer_schema(frame, max_rows=12)
    assert [column.dtype for column in schema.columns] == [
        "integer",
        "float",
        "category",
        "datetime",
    ]
    assert schema.columns[0].unique
    assert schema.columns[0].minimum == 1
    assert schema.columns[0].maximum == 3
    assert schema.columns[2].categories == ["a", "b"]
    assert schema.columns[3].timezone == "UTC"
    assert schema.max_rows == 12


def test_infer_rejects_columnless_frame() -> None:
    with pytest.raises(ValueError, match="no columns"):
        infer_schema(pa.table({}))


def test_arrow_schema_preserves_name_nullable_and_timezone() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="id", dtype="int32", nullable=False),
            ColumnSchema(name="at", dtype="datetime", timezone="UTC"),
        ]
    )
    result = arrow_schema(schema)
    assert result.names == ["id", "at"]
    assert result.field("id").type == pa.int32()
    assert not result.field("id").nullable
    assert result.field("at").type == pa.timestamp("us", tz="UTC")


def test_table_from_rows_retains_empty_dtypes() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="id", dtype="integer", nullable=False)])
    table = table_from_rows(schema, [])
    assert table.num_rows == 0
    assert table.schema.field("id").type == pa.int64()


def test_table_from_rows_supports_temporal_values() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="day", dtype="date")])
    table = table_from_rows(schema, [{"day": dt.date(2024, 2, 29)}])
    assert table.column("day").to_pylist() == [dt.date(2024, 2, 29)]


def test_table_from_rows_rejects_values_outside_declared_dtype() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="id", dtype="int64")])
    with pytest.raises(ValueError, match="declared dtype"):
        table_from_rows(schema, [{"id": "not-an-integer"}])
