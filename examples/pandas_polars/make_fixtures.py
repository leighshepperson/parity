"""Generate tiny, synthetic Parquet fixtures for the passing example campaign."""

from __future__ import annotations

from pathlib import Path

try:
    from .faults import make_demo_inputs
except ImportError:  # Allow direct `python examples/pandas_polars/make_fixtures.py`.
    from faults import make_demo_inputs


def main() -> None:
    destination = Path(__file__).parent / "fixtures"
    destination.mkdir(exist_ok=True)
    names = {
        "null-join": "null_join.parquet",
        "groupby-null": "groupby_null.parquet",
        "timezone-day": "timezone_day.parquet",
        "dtype-width": "dtype_width.parquet",
        "stable-order": "stable_order.parquet",
        "integer-precision": "integer_precision.parquet",
    }
    for name, frame in make_demo_inputs().items():
        frame.to_parquet(destination / names[name], index=False)


if __name__ == "__main__":
    main()
