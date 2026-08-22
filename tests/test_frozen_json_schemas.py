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
    "config": "f8259db9c527b66446882723bdd8b39c603557cb41d90a2df821e19b622d5471",
    "distilled-contract": "4e03243656358a300f086fecea2bf437cd4f4cd56889016bf79ffc9bfa5c7c52",
    "finding": "684830fd1a669390c67a9d3dbf52899e96540d49c78ddc7501558a02d9b3ed0b",
    "migration-manifest": "125f07d0506531e10ef094ce7eca0731f8788ee2eb89fcb32ba85e9b29998c01",
    "migration-report": "6b167ed4242cff1d44694a3edfd4a43be5f0b7fc5ae825c9111399cb772f01de",
    "replay": "f72b07dbaec152b47a029139f3f42b91277f90aae1dc0c5b9f70b6918de17640",
    "suite-report": "bf59966f9b5e222f792a9cc5bedda5d0ac7cf236c2270c5fb65991fc1004be03",
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
