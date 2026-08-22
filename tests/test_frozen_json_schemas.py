from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from parity.json_contracts import contract_names, contract_schema

FROZEN_SCHEMA_SHA256: dict[str, str] = {
    # Updated only when the corresponding public contract version changes.
    "agent-result": "4788dd74f652c966a5027ecf54c6d7aa10a53f5e507529059b29dcc08df9eb5f",
    "artifact-manifest": "c932308531ed02d30d21e5a86c1203abd69de39405e6262f246dec9949840a1a",
    "checklist": "f61d43610a6fab75938bdad6eafb68f6265c96fbfd5a01b42b9cd46bc4f2e628",
    "compatibility-budget": "5eaeff824ec67a3e72e026de00161343f250689c6adb3cb653820c8ac51be1f7",
    "config": "b9f1b62e213f2c71df71f433f9e881ffc4c3a54a8a204e3038fa0ed246a74a64",
    "distilled-contract": "be276fae5e149a7a60a9ab3cf163bb8a0c27cc65030cebf56462a7ce263aceaf",
    "finding": "a6786da6fec5badf1cc90cbba6d58d2a14290030bfbd826193c49e2d4b52b4cc",
    "migration-manifest": "125f07d0506531e10ef094ce7eca0731f8788ee2eb89fcb32ba85e9b29998c01",
    "migration-report": "fa908e65a926d1211c3649602a2ec3c74e0bbabae9615a068fa346731382ecec",
    "replay": "f72b07dbaec152b47a029139f3f42b91277f90aae1dc0c5b9f70b6918de17640",
    "suite-report": "502abcc11ad8b4674698aad4eee36f508e496552709c0461e0e17b0387611c3a",
    "workspace": "0b2ddbabc858ba54e6432dcb8e3239660011d4df67db10e718c19a78d0c9ae8a",
}


def test_public_schemas_are_frozen_package_resources() -> None:
    assert tuple(sorted(FROZEN_SCHEMA_SHA256)) == contract_names()

    root = files("parity.schemas")
    for name, expected_sha256 in FROZEN_SCHEMA_SHA256.items():
        resource = root.joinpath(f"{name}.json").read_bytes()
        assert hashlib.sha256(resource).hexdigest() == expected_sha256
        assert contract_schema(name) == json.loads(resource)


def test_contract_schema_returns_an_isolated_copy() -> None:
    schema = contract_schema("finding")
    schema["title"] = "mutated by caller"

    assert contract_schema("finding")["title"] != "mutated by caller"
