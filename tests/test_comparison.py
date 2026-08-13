from __future__ import annotations

import datetime as dt
import decimal
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

import parity.comparison as comparison_module
from parity.canonical import CanonicalFrame, CanonicalSeries, canonicalize
from parity.comparison import compare, compare_observations, compare_result, mismatch_signature
from parity.models import ComparisonPolicy, Mismatch, MismatchKind


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


def test_unordered_matching_finds_a_bijection_across_overlapping_tolerances() -> None:
    left = pa.table({"x": [0.0, 0.15]})
    right = pa.table({"x": [0.075, -0.075]})

    assert (
        compare(
            left,
            right,
            ComparisonPolicy(row_order="ignore", rtol=0, atol=0.1),
        )
        == []
    )

    impossible = pa.table({"x": [0.075, 0.3]})
    assert compare(
        left,
        impossible,
        ComparisonPolicy(row_order="ignore", rtol=0, atol=0.1),
    )


@pytest.mark.parametrize(
    "right_values",
    [list(range(127, -1, -1)), [1] * 128],
    ids=["reordered-unique", "identical-duplicates"],
)
def test_unordered_exact_fast_path_uses_linear_row_comparisons(
    right_values: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    left_values = list(range(128)) if len(set(right_values)) > 1 else [1] * 128
    calls = 0
    original = comparison_module._row_equal

    def counted(
        left: tuple[object, ...], right: tuple[object, ...], policy: ComparisonPolicy
    ) -> bool:
        nonlocal calls
        calls += 1
        return original(left, right, policy)

    monkeypatch.setattr(comparison_module, "_row_equal", counted)

    assert (
        compare(
            pa.table({"value": left_values}),
            pa.table({"value": right_values}),
            ComparisonPolicy(row_order="ignore"),
        )
        == []
    )
    assert calls == len(left_values)


@pytest.mark.parametrize(
    ("left_values", "right_values"),
    [
        (list(range(1000)), [*range(999), -1]),
        (list(range(1000)), list(range(999))),
    ],
    ids=["one-changed-row", "one-missing-row"],
)
def test_unordered_partial_exact_matching_only_explores_unmatched_rows(
    left_values: list[int],
    right_values: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = comparison_module._row_equal

    def counted(
        left: tuple[object, ...], right: tuple[object, ...], policy: ComparisonPolicy
    ) -> bool:
        nonlocal calls
        calls += 1
        return original(left, right, policy)

    monkeypatch.setattr(comparison_module, "_row_equal", counted)

    mismatches = compare(
        pa.table({"value": left_values}),
        pa.table({"value": right_values}),
        ComparisonPolicy(row_order="ignore"),
    )

    assert mismatches
    assert calls < 4 * max(len(left_values), len(right_values))


def test_keyed_rows_align_by_single_or_composite_identity() -> None:
    left = pa.table({"account": ["a", "b"], "day": [1, 1], "amount": [10.0, 20.0]})
    reordered = pa.table({"account": ["b", "a"], "day": [1, 1], "amount": [20.0, 10.0]})

    assert (
        compare(
            left,
            reordered,
            ComparisonPolicy(row_order="keyed", row_keys=["account"]),
        )
        == []
    )
    assert (
        compare(
            left,
            reordered,
            ComparisonPolicy(row_order="keyed", row_keys=["account", "day"]),
        )
        == []
    )


def test_nested_mapping_frames_apply_keyed_row_alignment() -> None:
    left = {"result": pa.table({"id": [1, 2], "amount": [10.0, 20.0]})}
    reordered = {"result": pa.table({"id": [2, 1], "amount": [20.0, 10.0]})}
    policy = ComparisonPolicy(row_order="keyed", row_keys=["id"])

    assert compare(left, reordered, policy) == []

    changed = {"result": pa.table({"id": [2, 1], "amount": [21.0, 10.0]})}
    mismatch = compare(left, changed, policy)[0]
    assert mismatch.path == "$['result'][1].amount"
    assert mismatch.details["candidate_row"] == 0


def test_nested_sequence_frames_apply_unordered_row_alignment() -> None:
    left = [pa.table({"id": [1, 2], "amount": [10.0, 20.0]})]
    reordered = [pa.table({"id": [2, 1], "amount": [20.0, 10.0]})]

    assert (
        compare(
            left,
            reordered,
            ComparisonPolicy(row_order="ignore"),
        )
        == []
    )


def test_canonical_frame_and_series_are_canonicalization_fixed_points() -> None:
    frame = canonicalize(pa.table({"id": [1]}))
    series = canonicalize(pd.Series([1], name="id"))

    assert isinstance(frame, CanonicalFrame)
    assert isinstance(series, CanonicalSeries)
    assert canonicalize(frame) is frame
    assert canonicalize(series) is series


def test_keyed_alignment_reports_precise_cell_and_row_metadata() -> None:
    left = pa.table({"id": [1, 2], "amount": [10.0, 20.0]})
    right = pa.table({"id": [2, 1], "amount": [21.0, 10.0]})

    mismatch = compare(
        left,
        right,
        ComparisonPolicy(row_order="keyed", row_keys=["id"]),
    )[0]

    assert mismatch.kind is MismatchKind.VALUE
    assert mismatch.path == "$[1].amount"
    assert mismatch.details == {
        "rtol": 1e-7,
        "atol": 0.0,
        "alignment": "keyed",
        "key_columns": ["id"],
        "reference_row": 1,
        "candidate_row": 0,
    }


def test_keyed_alignment_reports_missing_unexpected_and_unavailable_keys() -> None:
    policy = ComparisonPolicy(row_order="keyed", row_keys=["id"])
    missing = compare(
        pa.table({"id": [1, 2], "x": [10, 20]}),
        pa.table({"id": [1, 3], "x": [10, 30]}),
        policy,
    )
    assert [mismatch.message for mismatch in missing] == [
        "reference row key has no candidate row",
        "candidate row key has no reference row",
    ]
    assert [mismatch.path for mismatch in missing] == ["$[1]", "$candidate[1]"]

    unavailable = compare(
        pa.table({"value": [1]}),
        pa.table({"value": [1]}),
        policy,
    )
    assert unavailable[0].message == "configured row key columns are unavailable"

    unequal = compare(
        pa.table({"id": [1, 2], "x": [10, 20]}),
        pa.table({"id": [1], "x": [10]}),
        policy,
        max_mismatches=1,
    )
    assert [mismatch.message for mismatch in unequal] == ["reference row key has no candidate row"]


def test_keyed_alignment_honours_column_order_independently() -> None:
    left = pa.table({"id": [1], "value": [10]})
    right = pa.table({"value": [10], "id": [1]})
    strict = compare(
        left,
        right,
        ComparisonPolicy(row_order="keyed", row_keys=["id"]),
    )
    assert [m.message for m in strict] == ["column order differs"]
    assert (
        compare(
            left,
            right,
            ComparisonPolicy(row_order="keyed", row_keys=["id"], column_order="ignore"),
        )
        == []
    )


@pytest.mark.parametrize("side", ["reference", "candidate"])
def test_keyed_alignment_fails_closed_on_duplicate_keys(side: str) -> None:
    unique = pa.table({"id": [1, 2], "x": [10, 20]})
    duplicate = pa.table({"id": [1, 1], "x": [10, 20]})
    left, right = (duplicate, unique) if side == "reference" else (unique, duplicate)

    mismatch = compare(
        left,
        right,
        ComparisonPolicy(row_order="keyed", row_keys=["id"]),
    )[0]

    assert mismatch.message == "row keys are not unique"
    assert mismatch.path == f"${side}[1]"
    assert mismatch.details["side"] == side


def test_keyed_identity_is_exact_and_numeric_without_lossy_float_conversion() -> None:
    # Value tolerance applies only after identity alignment.
    left = pa.table({"id": [1.0], "value": [1.0]})
    near_key = pa.table({"id": [1.01], "value": [1.0]})
    policy = ComparisonPolicy(row_order="keyed", row_keys=["id"], atol=1.0, rtol=0)
    assert {m.message for m in compare(left, near_key, policy)} == {
        "reference row key has no candidate row",
        "candidate row key has no reference row",
    }

    # Compatible exact numeric identities align across physical numeric types.
    assert (
        compare(
            pa.table({"id": pa.array([1], type=pa.int64()), "value": [1]}),
            pa.table({"id": pa.array([1.0], type=pa.float64()), "value": [1]}),
            ComparisonPolicy(row_order="keyed", row_keys=["id"]),
        )
        == []
    )

    large = 2**60 + 1
    assert compare(
        pa.table({"id": [large], "value": [1]}),
        pa.table({"id": [2**60], "value": [1]}),
        ComparisonPolicy(row_order="keyed", row_keys=["id"]),
    )


def test_keyed_identity_respects_missing_and_signed_zero_policies() -> None:
    zeros = pa.table({"id": [0.0, -0.0], "value": [1, 2]})
    equal_zero = compare(
        zeros,
        zeros,
        ComparisonPolicy(row_order="keyed", row_keys=["id"]),
    )
    assert equal_zero[0].message == "row keys are not unique"
    assert (
        compare(
            zeros,
            zeros,
            ComparisonPolicy(row_order="keyed", row_keys=["id"], signed_zero_equal=False),
        )
        == []
    )

    null_key = pa.table({"id": pa.array([None], type=pa.float64()), "value": [1]})
    assert (
        compare(
            null_key,
            null_key,
            ComparisonPolicy(row_order="keyed", row_keys=["id"], null_equal=False),
        )[0].message
        == "row key is not alignable under the comparison policy"
    )

    nan_key = pa.table(
        {"id": pa.array([float("nan")], type=pa.float64(), from_pandas=False), "value": [1]}
    )
    assert (
        compare(
            nan_key,
            nan_key,
            ComparisonPolicy(row_order="keyed", row_keys=["id"], nan_equal=False),
        )[0].message
        == "row key is not alignable under the comparison policy"
    )


def test_keyed_identity_keeps_boolean_distinct_and_handles_casefolded_columns() -> None:
    boolean = pa.table({"ID": [True], "value": [1]})
    integer = pa.table({"id": [1], "value": [1]})
    mismatches = compare(
        boolean,
        integer,
        ComparisonPolicy(
            row_order="keyed",
            row_keys=["id"],
            names="case_insensitive",
            dtype="ignore",
        ),
    )
    assert {m.message for m in mismatches} == {
        "reference row key has no candidate row",
        "candidate row key has no reference row",
    }


def test_keyed_composite_tokens_cannot_collide_through_delimiters() -> None:
    left = pa.table({"a": ["x|y"], "b": ["z"], "value": [1]})
    right = pa.table({"a": ["x"], "b": ["y|z"], "value": [1]})
    mismatches = compare(
        left,
        right,
        ComparisonPolicy(row_order="keyed", row_keys=["a", "b"]),
    )
    assert len(mismatches) == 2


def test_keyed_null_nan_coalescing_can_expose_duplicate_identity() -> None:
    frame = pa.table(
        {
            "id": pa.array([None, float("nan")], type=pa.float64(), from_pandas=False),
            "value": [1, 2],
        }
    )
    mismatches = compare(
        frame,
        frame,
        ComparisonPolicy(row_order="keyed", row_keys=["id"], null_nan_equal=True),
    )
    assert mismatches[0].message == "row keys are not unique"


def test_keyed_alignment_rejects_nested_key_values() -> None:
    frame = pa.table({"id": [[1]], "value": [1]})
    mismatch = compare(
        frame,
        frame,
        ComparisonPolicy(row_order="keyed", row_keys=["id"]),
    )[0]
    assert mismatch.message == "row key contains a non-scalar value"


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


def test_pandas_timestamp_and_timedelta_keep_nanosecond_precision() -> None:
    timestamp = pd.Timestamp("2024-01-01 00:00:00.000000001")
    duration = pd.Timedelta(1, unit="ns")
    assert compare(timestamp, timestamp + pd.Timedelta(1, unit="ns"))
    assert compare(duration, duration + pd.Timedelta(1, unit="ns"))
    assert (
        compare(
            timestamp,
            timestamp + pd.Timedelta(1, unit="ns"),
            ComparisonPolicy(datetime_tolerance_ns=1),
        )
        == []
    )


def test_keyed_numpy_datetime_and_duration_scalars_keep_exact_identity() -> None:
    policy = ComparisonPolicy(row_order="keyed", row_keys=["id"])
    dates = pa.table(
        {"id": pa.array([np.datetime64("2024-01-01T00:00:00.000000001")]), "value": [1]}
    )
    durations = pa.table({"id": pa.array([np.timedelta64(1, "ns")]), "value": [1]})

    assert compare(dates, dates, policy) == []
    assert compare(durations, durations, policy) == []


def test_keyed_decimal_and_float_identity_is_mathematically_exact() -> None:
    # Decimal 0.5 and binary float 0.5 denote the same rational value.
    left = pd.DataFrame({"id": [decimal.Decimal("0.5")], "value": [1]})
    right = pd.DataFrame({"id": [0.5], "value": [1]})
    assert (
        compare(
            left,
            right,
            ComparisonPolicy(row_order="keyed", row_keys=["id"]),
        )
        == []
    )

    # Finite Decimals outside float range must not collapse to infinity.
    policy = ComparisonPolicy(row_order="keyed", row_keys=["id"])
    assert comparison_module._key_token(
        decimal.Decimal("1e10000"), policy
    ) != comparison_module._key_token(decimal.Decimal("Infinity"), policy)


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


def test_observation_comparison_reports_mutated_bundle_inputs_by_label() -> None:
    returned = "ExecutionOutcome.RETURNED"
    left = SimpleNamespace(
        outcome=returned,
        table=pa.table({"x": [1]}),
        has_value=False,
        mutated_input=True,
        mutated_inputs=("orders",),
    )
    right = SimpleNamespace(
        outcome=returned,
        table=pa.table({"x": [1]}),
        has_value=False,
        mutated_input=True,
        mutated_inputs=("customers",),
    )

    mismatches = compare_observations(left, right)

    assert [mismatch.path for mismatch in mismatches] == [
        "$inputs/customers",
        "$inputs/orders",
    ]
    assert mismatches[0].details == {"input": "customers"}


def test_observation_comparison_fails_closed_on_inconsistent_mutation_metadata() -> None:
    returned = "ExecutionOutcome.RETURNED"
    inconsistent = SimpleNamespace(
        outcome=returned,
        table=pa.table({"x": [1]}),
        has_value=False,
        mutated_input=False,
        mutated_inputs=("orders",),
    )
    stable = SimpleNamespace(
        outcome=returned,
        table=pa.table({"x": [1]}),
        has_value=False,
        mutated_input=False,
        mutated_inputs=(),
    )

    mismatches = compare_observations(inconsistent, stable)

    assert mismatches[0].message == "input mutation metadata is inconsistent"
    assert mismatches[0].path == "$inputs"


def test_mismatch_signature_is_stable_across_values_indices_and_secondary_symptoms() -> None:
    first = Mismatch(
        kind=MismatchKind.VALUE,
        message="numeric values differ beyond tolerance",
        path="$[1].amount",
        reference=10,
        candidate=11,
        details={"atol": 0.0},
    )
    another_witness = Mismatch(
        kind=MismatchKind.VALUE,
        message="numeric values differ beyond tolerance",
        path="$[99].amount",
        reference=999,
        candidate=-1,
        details={"atol": 100.0},
    )
    secondary = Mismatch(
        kind=MismatchKind.VALUE,
        message="values differ",
        path="$[0].note",
    )

    assert mismatch_signature([first]) == mismatch_signature([another_witness])
    assert mismatch_signature([first, secondary]) == mismatch_signature([first])
    assert mismatch_signature([first]).startswith("ms1:")
    assert len(mismatch_signature([first])) == 68


def test_mismatch_signature_elides_field_names_and_keeps_primary_contract_distinctions() -> None:
    amount = Mismatch(
        kind=MismatchKind.VALUE,
        message="values differ",
        path="$[0].amount",
    )
    currency = amount.model_copy(update={"path": "$[0].currency"})
    shape = Mismatch(kind=MismatchKind.SHAPE, message="row counts differ", path="$")

    assert mismatch_signature([amount]) == mismatch_signature([currency])
    assert mismatch_signature([amount, shape]) == mismatch_signature([shape, amount])


def test_mismatch_signature_conservatively_groups_unknown_messages_and_rejects_empty() -> None:
    first = Mismatch(kind=MismatchKind.VALUE, message="dynamic 123", path="$[0].x")
    second = Mismatch(kind=MismatchKind.VALUE, message="dynamic 987", path="$[4].x")

    assert mismatch_signature([first]) == mismatch_signature([second])
    with pytest.raises(ValueError, match="at least one mismatch"):
        mismatch_signature([])


def test_keyed_structural_signatures_ignore_values_but_keep_failure_shapes() -> None:
    first = Mismatch(
        kind=MismatchKind.ROW,
        message="reference row key has no candidate row",
        path="$[1]",
        details={"key_columns": ["private-id"]},
    )
    another = first.model_copy(
        update={"path": "$[999]", "details": {"key_columns": ["another-secret"]}}
    )
    duplicate = Mismatch(
        kind=MismatchKind.ROW,
        message="row keys are not unique",
        path="$reference[2]",
    )

    assert mismatch_signature([first]) == mismatch_signature([another])
    assert mismatch_signature([first]) != mismatch_signature([duplicate])


def test_mapping_signature_is_stable_across_python_hash_seeds() -> None:
    script = textwrap.dedent(
        """
        from parity.comparison import compare, mismatch_signature

        reference = {f"key-{index}": "left" for index in range(200)}
        candidate = {f"key-{index}": "right" for index in range(200)}
        reference["rare-boolean"] = True
        candidate["rare-boolean"] = 1
        print(mismatch_signature(compare(reference, candidate)))
        """
    )
    project_root = Path(__file__).parents[1]
    signatures = []
    for hash_seed in ("1", "3"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = hash_seed
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(project_root / "src"), environment.get("PYTHONPATH")))
        )
        signatures.append(
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=project_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

    assert signatures[0] == signatures[1]


def test_shape_and_column_set_mismatches_are_not_generic_values() -> None:
    shape = compare(pa.table({"x": [1]}), pa.table({"x": [1, 2]}))[0]
    assert shape.kind is MismatchKind.SHAPE
    columns = compare(pa.table({"x": [1]}), pa.table({"y": [1]}))[0]
    assert columns.kind is MismatchKind.COLUMN
