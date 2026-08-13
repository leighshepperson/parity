from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import polars as pl
import pyarrow as pa

from parity.comparison import compare, compare_observations, compare_result
from parity.models import ComparisonPolicy, MismatchKind


def test_cross_library_frames_are_compatible_by_default() -> None:
    pandas_frame = pd.DataFrame({"id": [1, 2], "value": pd.Series([1.0, pd.NA], dtype="Float64")})
    polars_frame = pl.DataFrame({"id": [1, 2], "value": [1.0, None]})
    assert compare(pandas_frame, polars_frame) == []
    assert compare_result(pandas_frame, polars_frame).equivalent


def test_cross_library_nan_is_not_conflated_with_null() -> None:
    pandas_nan = pd.DataFrame({"x": [float("nan")]})
    polars_nan = pl.DataFrame({"x": [float("nan")]})
    polars_null = pl.DataFrame({"x": pl.Series("x", [None], dtype=pl.Float64)})
    assert compare(pandas_nan, polars_nan) == []
    assert compare(pandas_nan, polars_null)


def test_reports_cell_path_and_tolerance() -> None:
    left = pa.table({"x": [1.0, 2.0]})
    near = pa.table({"x": [1.0, 2.0000001]})
    far = pa.table({"x": [1.0, 2.1]})
    policy = ComparisonPolicy(rtol=1e-5)
    assert compare(left, near, policy) == []
    mismatch = compare(left, far, policy)[0]
    assert mismatch.kind is MismatchKind.VALUE
    assert mismatch.path == "$[1].x"


def test_row_and_column_order_are_independent() -> None:
    left = pa.table({"a": [1, 2], "b": ["x", "y"]})
    reordered = pa.table({"b": ["y", "x"], "a": [2, 1]})
    strict = compare(left, reordered)
    assert any(mismatch.message == "column order differs" for mismatch in strict)
    ignored = ComparisonPolicy(column_order="ignore", row_order="ignore")
    assert compare(left, reordered, ignored) == []


def test_duplicate_rows_match_as_multisets_when_order_ignored() -> None:
    left = pa.table({"x": [1, 1, 2]})
    right = pa.table({"x": [2, 1, 1]})
    assert compare(left, right, ComparisonPolicy(row_order="ignore")) == []
    missing = pa.table({"x": [2, 2, 1]})
    assert compare(left, missing, ComparisonPolicy(row_order="ignore"))[0].kind is MismatchKind.ROW


def test_dtype_modes_and_ignored_columns() -> None:
    left = pa.table({"id": pa.array([1], type=pa.int32()), "run": [1]})
    right = pa.table({"id": pa.array([1], type=pa.int64()), "run": [99]})
    assert compare(left, right, ComparisonPolicy(ignored_columns=["run"])) == []
    mismatch = compare(left, right, ComparisonPolicy(dtype="strict", ignored_columns=["run"]))[0]
    assert mismatch.kind is MismatchKind.DTYPE


def test_compatible_dtype_accepts_all_null_inferred_against_typed_nulls() -> None:
    inferred_null = pa.table({"value": pa.nulls(1)})
    typed_null = pa.table({"value": pa.array([None], type=pa.float64())})
    assert compare(inferred_null, typed_null, ComparisonPolicy(dtype="compatible")) == []
    assert compare(inferred_null, typed_null, ComparisonPolicy(dtype="strict"))


def test_null_nan_equivalence_applies_to_compatible_all_missing_dtype() -> None:
    inferred_null = pa.table({"value": pa.nulls(1)})
    floating_nan = pa.table(
        {"value": pa.array([float("nan")], type=pa.float64(), from_pandas=False)}
    )

    assert compare(inferred_null, floating_nan)
    assert (
        compare(
            inferred_null,
            floating_nan,
            ComparisonPolicy(dtype="compatible", null_nan_equal=True),
        )
        == []
    )


def test_null_nan_and_signed_zero_policies() -> None:
    assert compare(None, None) == []
    assert compare(None, None, ComparisonPolicy(null_equal=False))
    assert compare(float("nan"), float("nan")) == []
    assert compare(float("nan"), float("nan"), ComparisonPolicy(nan_equal=False))
    assert compare(None, float("nan"))
    assert compare(None, float("nan"), ComparisonPolicy(null_nan_equal=True)) == []
    assert compare(float("nan"), None, ComparisonPolicy(null_nan_equal=True)) == []
    # Cross-kind equivalence does not override the policies for two values of
    # the same missing kind.
    assert compare(None, None, ComparisonPolicy(null_nan_equal=True, null_equal=False))
    assert compare(
        float("nan"),
        float("nan"),
        ComparisonPolicy(null_nan_equal=True, nan_equal=False),
    )
    assert compare(0.0, -0.0) == []
    assert compare(0.0, -0.0, ComparisonPolicy(signed_zero_equal=False))


def test_nested_scalars_and_datetime_tolerance() -> None:
    left = {"scores": [1.0, 2.0], "meta": {"active": True}}
    right = {"scores": [1.0, 2.00000001], "meta": {"active": True}}
    assert compare(left, right) == []
    at = dt.datetime(2024, 1, 1)
    later = at + dt.timedelta(microseconds=1)
    assert compare(at, later, ComparisonPolicy(datetime_tolerance_ns=1_000)) == []
    assert compare(True, 1)[0].kind is MismatchKind.VALUE


def test_exceptions_are_structured_and_checked() -> None:
    left = ValueError("bad input")
    assert compare(left, ValueError("bad input")) == []
    mismatch = compare(left, TypeError("bad input"))[0]
    assert mismatch.kind is MismatchKind.EXCEPTION
    assert compare(left, TypeError("different"), ComparisonPolicy(check_exceptions=False)) == []


def test_observation_comparison_is_duck_typed_and_checks_mutation() -> None:
    returned = "ExecutionOutcome.RETURNED"
    left = SimpleNamespace(
        outcome=returned, table=pa.table({"x": [1]}), has_value=False, mutated_input=False
    )
    right = SimpleNamespace(
        outcome=returned, table=pa.table({"x": [1]}), has_value=False, mutated_input=True
    )
    mismatches = compare_observations(left, right)
    assert [mismatch.kind for mismatch in mismatches] == [MismatchKind.MUTATION]


def test_shape_and_column_set_mismatches_are_not_generic_values() -> None:
    shape = compare(pa.table({"x": [1]}), pa.table({"x": [1, 2]}))[0]
    assert shape.kind is MismatchKind.SHAPE
    columns = compare(pa.table({"x": [1]}), pa.table({"y": [1]}))[0]
    assert columns.kind is MismatchKind.COLUMN
