"""Regenerate Parity's checked-in JSON Schema resources deliberately.

Run this only when a published contract version is being changed.  Runtime
schema discovery reads the resulting package resources; it never asks the
installed Pydantic version to regenerate a versioned schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from parity.json_contracts import _generated_contract_schema, contract_names


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "src" / "parity" / "schemas"
    destination.mkdir(parents=True, exist_ok=True)
    for name in contract_names():
        schema = _generated_contract_schema(name)
        (destination / f"{name}.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
