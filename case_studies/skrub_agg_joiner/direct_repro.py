"""Backend-native reproduction of the three focused skrub findings."""

from __future__ import annotations

import hashlib
import inspect
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import skrub._agg_joiner
from skrub import AggJoiner
from skrub._agg_joiner import aggregate

TARGET_SHA256 = "ece034198b746e0d08e34c0e62ba2c892ac6be04d5710ce9c57d9ec967b51b7b"


def heading(title: str) -> None:
    print(f"\n## {title}")


def verify_target() -> None:
    source = inspect.getsourcefile(skrub._agg_joiner)
    if source is None:
        raise RuntimeError("could not locate skrub._agg_joiner")
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if digest != TARGET_SHA256:
        raise RuntimeError(
            "skrub._agg_joiner does not match the pinned study commit: "
            f"expected {TARGET_SHA256}, got {digest}"
        )
    print(f"target_sha256={digest}")


def print_versions() -> None:
    for distribution in (
        "skrub",
        "scikit-learn",
        "pandas",
        "polars",
        "pyarrow",
        "numpy",
    ):
        print(f"{distribution}={version(distribution)}")


heading("Pinned target and versions")
verify_target()
print_versions()

heading("Durable finding: null grouping keys")
pandas_null_key = pd.DataFrame({"key": ["A", None, "A"], "value": [1.0, 2.0, 3.0]})
polars_null_key = pl.DataFrame({"key": ["A", None, "A"], "value": [1.0, 2.0, 3.0]})
pandas_null_result = aggregate(pandas_null_key, ["key"], ["value"], ["count", "sum", "mean"], "")
polars_null_result = aggregate(polars_null_key, ["key"], ["value"], ["count", "sum", "mean"], "")
print("pandas:")
print(pandas_null_result)
print("polars:")
print(polars_null_result)
assert len(pandas_null_result) == 1
assert len(polars_null_result) == 2

heading("Durable finding: tied mode through public AggJoiner")
tied = {"key": ["A"] * 4, "label": ["x", "y", "x", "y"]}
pandas_mode_result = AggJoiner(
    aux_table="X", operations="mode", key="key", cols="label"
).fit_transform(pd.DataFrame(tied))
polars_mode_result = AggJoiner(
    aux_table="X", operations="mode", key="key", cols="label"
).fit_transform(pl.DataFrame(tied))
print("pandas:")
print(pandas_mode_result)
print("polars:")
print(polars_mode_result)
pandas_mode = pandas_mode_result["label_mode"].iloc[0]
polars_mode = polars_mode_result["label_mode"][0]
assert set(pandas_mode) == {"x", "y"}
assert polars_mode in {"x", "y"}
assert not isinstance(polars_mode, (list, tuple, np.ndarray))

tied_polars = pl.DataFrame(tied)
choices = [
    aggregate(tied_polars, ["key"], ["label"], ["mode"], "")["label_mode"][0] for _ in range(20)
]
print(f"polars choices across 20 identical evaluations: {choices}")

heading("Version-sensitive finding: IEEE NaN through public AggJoiner")
nan_data = {
    "key": ["A", "A", "B", "B"],
    "value": [1.0, float("nan"), float("nan"), 2.0],
}
pandas_nan_result = AggJoiner(
    aux_table="X",
    operations=["count", "sum", "mean"],
    key="key",
    cols="value",
).fit_transform(pd.DataFrame(nan_data))
polars_nan_result = AggJoiner(
    aux_table="X",
    operations=["count", "sum", "mean"],
    key="key",
    cols="value",
).fit_transform(pl.DataFrame(nan_data))
print("pandas:")
print(pandas_nan_result)
print("polars:")
print(polars_nan_result)
assert pandas_nan_result["value_count"].tolist() == [1, 1, 1, 1]
assert polars_nan_result["value_count"].to_list() == [2, 2, 2, 2]
assert pandas_nan_result["value_sum"].tolist() == [1.0, 1.0, 2.0, 2.0]
assert all(np.isnan(value) for value in polars_nan_result["value_sum"].to_list())

print("\nAll direct assertions reproduced.")
