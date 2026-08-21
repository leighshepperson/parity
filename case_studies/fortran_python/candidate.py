"""Python ports of the Fortran compensated-summation contract."""

from __future__ import annotations

import math

import pyarrow as pa


def _values(table: pa.Table) -> list[float]:
    if table.column_names != ["value"] or table.num_rows < 1:
        raise ValueError("expected one non-empty value column")
    if table.schema.field("value").type != pa.float64():
        raise TypeError("value must use Arrow float64")
    values = table.column("value").to_pylist()
    if any(value is None or not math.isfinite(value) for value in values):
        raise ValueError("value must contain only finite non-null numbers")
    return values


def correct_port(table: pa.Table) -> float:
    """Preserve the reference's ordered Neumaier compensation."""

    total = 0.0
    correction = 0.0
    for value in _values(table):
        updated = total + value
        if abs(total) >= abs(value):
            correction += (total - updated) + value
        else:
            correction += (value - updated) + total
        total = updated
    return total + correction


def naive_port(table: pa.Table) -> float:
    """Deliberate migration defect: discard compensation during the rewrite."""

    total = 0.0
    for value in _values(table):
        total += value
    return total
