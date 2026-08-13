from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

import pandas as pd
import polars as pl
from examples.pandas_polars import corrected, faults

from parity.models import ParityConfig

ROOT = Path(__file__).parents[1]


def _pandas(frame: pl.DataFrame) -> pd.DataFrame:
    return frame.to_pandas(use_pyarrow_extension_array=False)


def test_fault_configs_parse_and_targets_import() -> None:
    example_dir = ROOT / "examples" / "pandas_polars"
    sys.path.insert(0, str(example_dir))
    for name in ("parity.toml", "parity.fixed.toml"):
        raw = tomllib.loads((example_dir / name).read_text())
        config = ParityConfig.model_validate(raw)
        assert config.cases
        for case in config.cases:
            module_name, attribute = case.reference.target.split(":")
            module = importlib.import_module(module_name)
            assert callable(getattr(module, attribute))
            module_name, attribute = case.candidate.target.split(":")
            module = importlib.import_module(module_name)
            assert callable(getattr(module, attribute))
    sys.path.remove(str(example_dir))


def test_fixed_corpus_uses_explicit_bounded_generation_domains() -> None:
    example_dir = ROOT / "examples" / "pandas_polars"
    raw = tomllib.loads((example_dir / "parity.fixed.toml").read_text())
    config = ParityConfig.model_validate(raw)
    assert all(case.input_schema is not None for case in config.cases)
    assert all(case.generation.max_examples <= 3 for case in config.cases)
    timezone = next(case for case in config.cases if case.name == "timezone-day-fixed")
    assert timezone.input_schema is not None
    assert timezone.input_schema.columns[0].categories


def test_bad_join_loses_null_match_and_fixed_preserves_it() -> None:
    frame = faults.make_demo_inputs()["null-join"]
    reference = faults.join_reference(frame)
    bad = _pandas(faults.join_bad(pl.from_pandas(frame)))
    fixed = _pandas(corrected.join_fixed(pl.from_pandas(frame)))
    assert reference["segment"].tolist() != bad["segment"].tolist()
    assert reference["segment"].tolist() == fixed["segment"].tolist()


def test_bad_groupby_drops_null_bucket_and_fixed_retains_it() -> None:
    frame = faults.make_demo_inputs()["groupby-null"]
    reference = faults.groupby_reference(frame)
    bad = _pandas(faults.groupby_bad(pl.from_pandas(frame)))
    fixed = _pandas(corrected.groupby_fixed(pl.from_pandas(frame)))
    assert len(reference) == 2
    assert len(bad) == 1
    assert len(fixed) == 2


def test_fixed_groupby_preserves_null_and_nan_as_distinct_missing_values() -> None:
    null_result = corrected.groupby_fixed(
        pl.DataFrame(
            {"region": ["north"], "amount": [None]},
            schema={"region": pl.String, "amount": pl.Float64},
        )
    )
    nan_result = corrected.groupby_fixed(
        pl.DataFrame({"region": ["north"], "amount": [float("nan")]})
    )
    assert null_result["amount"][0] is None
    assert nan_result["amount"][0] != nan_result["amount"][0]


def test_bad_timezone_uses_utc_day_and_fixed_uses_local_day() -> None:
    frame = faults.make_demo_inputs()["timezone-day"]
    reference = faults.timezone_reference(frame)
    bad = _pandas(faults.timezone_bad(pl.from_pandas(frame)))
    fixed = _pandas(corrected.timezone_fixed(pl.from_pandas(frame)))
    assert reference.iloc[0, 0] == "2025-12-31"
    assert bad.iloc[0, 0] == "2026-01-01"
    assert fixed.iloc[0, 0] == reference.iloc[0, 0]


def test_bad_dtype_narrows_out_of_range_and_fixed_retains_value() -> None:
    frame = faults.make_demo_inputs()["dtype-width"]
    reference = faults.dtype_reference(frame)
    bad = _pandas(faults.dtype_bad(pl.from_pandas(frame)))
    fixed = _pandas(corrected.dtype_fixed(pl.from_pandas(frame)))
    assert pd.isna(bad.iloc[0, 0])
    assert reference.iloc[0, 0] == fixed.iloc[0, 0] == 128


def test_bad_ordering_changes_ties_and_fixed_is_stable() -> None:
    frame = faults.make_demo_inputs()["stable-order"]
    reference = faults.ordering_reference(frame)
    bad = _pandas(faults.ordering_bad(pl.from_pandas(frame)))
    fixed = _pandas(corrected.ordering_fixed(pl.from_pandas(frame)))
    assert reference["record_id"].tolist() == [30, 20, 10]
    assert bad["record_id"].tolist() == [30, 10, 20]
    assert fixed["record_id"].tolist() == reference["record_id"].tolist()


def test_example_module_and_docs_are_clean_room_synthetic() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "examples").rglob("*.py")
    ).lower()
    assert "g-research" not in content
    assert "supabase" not in content
    assert "customer_id" in content
