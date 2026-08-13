from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest
from pydantic import ValidationError

from parity import __version__
from parity._version import __version__ as source_version
from parity.provenance import (
    CORE_DISTRIBUTIONS,
    MAX_RECORDED_DISTRIBUTIONS,
    DistributionProvenance,
    RuntimeProvenance,
    _collect_runtime_provenance_cached,
    collect_runtime_provenance,
    diff_runtime,
    effective_config_sha256,
    normalize_distribution_name,
    normalize_distribution_names,
)


def _runtime(
    *, python_version: str = "3.12.1", distributions: tuple[DistributionProvenance, ...] = ()
) -> RuntimeProvenance:
    return RuntimeProvenance(
        python_implementation="CPython",
        python_version=python_version,
        platform_system="Linux",
        platform_machine="x86_64",
        parity_version="0.1.0",
        distributions=distributions,
    )


def test_public_version_uses_single_source() -> None:
    assert __version__ == source_version == "0.7.0"


def test_distribution_names_are_normalized_and_bounded() -> None:
    assert normalize_distribution_name("Scikit_Learn") == "scikit-learn"
    assert normalize_distribution_names(["skrub", "Scikit_Learn"]) == (
        "scikit-learn",
        "skrub",
    )
    with pytest.raises(ValueError, match="duplicate"):
        normalize_distribution_names(["Scikit-Learn", "scikit_learn"])
    with pytest.raises(ValueError, match="ASCII"):
        normalize_distribution_name("private/package")
    with pytest.raises(TypeError, match="not a string"):
        normalize_distribution_names("pandas")
    with pytest.raises(ValueError, match=str(MAX_RECORDED_DISTRIBUTIONS)):
        normalize_distribution_names(f"package-{index}" for index in range(65))


def test_provenance_models_reject_inconsistent_or_unsafe_metadata() -> None:
    with pytest.raises(ValidationError, match="requires a version"):
        DistributionProvenance(name="demo", status="installed")
    with pytest.raises(ValidationError, match="only an installed"):
        DistributionProvenance(name="demo", status="missing", version="1.0")
    with pytest.raises(ValidationError, match="normalized"):
        DistributionProvenance(name="Demo_Package", status="missing")
    with pytest.raises(ValidationError, match="unsupported characters"):
        DistributionProvenance(name="demo", status="installed", version="API_KEY=/tmp/key")

    item = DistributionProvenance(name="demo", status="installed", version="1.0")
    with pytest.raises(ValidationError, match="sorted"):
        _runtime(
            distributions=(
                DistributionProvenance(name="z-demo", status="missing"),
                item,
            )
        )


def test_collector_records_core_and_explicit_versions_without_raw_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collect_runtime_provenance_cached.cache_clear()
    versions = dict.fromkeys(CORE_DISTRIBUTIONS, "1.2.3")
    versions["skrub"] = "0.11.dev0"

    def fake_version(name: str) -> str:
        if name == "not-installed":
            raise importlib.metadata.PackageNotFoundError(name)
        if name == "broken-metadata":
            raise RuntimeError("/private/path API_TOKEN=do-not-record")
        if name == "unsafe-version":
            return "/private/version"
        return versions[name]

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    monkeypatch.setattr("parity.provenance.platform.python_implementation", lambda: "CPython")
    monkeypatch.setattr("parity.provenance.platform.python_version", lambda: "3.12.7")
    monkeypatch.setattr("parity.provenance.platform.system", lambda: "Linux")
    monkeypatch.setattr("parity.provenance.platform.machine", lambda: "aarch64")

    provenance = collect_runtime_provenance(
        ["skrub", "not-installed", "broken-metadata", "unsafe-version"]
    )
    recorded = {item.name: item for item in provenance.distributions}

    assert provenance.python_version == "3.12.7"
    assert provenance.platform_machine == "aarch64"
    assert set(CORE_DISTRIBUTIONS).issubset(recorded)
    assert recorded["skrub"].version == "0.11.dev0"
    assert recorded["not-installed"].status == "missing"
    assert recorded["broken-metadata"].status == "unavailable"
    assert recorded["unsafe-version"].status == "unavailable"
    serialized = provenance.model_dump_json()
    assert "/private" not in serialized
    assert "do-not-record" not in serialized


def test_collector_caches_one_runtime_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    _collect_runtime_provenance_cached.cache_clear()
    calls = 0

    def fake_version(_name: str) -> str:
        nonlocal calls
        calls += 1
        return "1.0"

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    first = collect_runtime_provenance(["demo"])
    count_after_first = calls
    second = collect_runtime_provenance(["demo"])
    assert first is second
    assert calls == count_after_first == len(CORE_DISTRIBUTIONS) + 1


def test_runtime_diff_returns_only_stable_component_paths() -> None:
    expected = _runtime(
        distributions=(DistributionProvenance(name="pandas", status="installed", version="2.3.3"),)
    )
    actual = _runtime(
        python_version="3.13.0",
        distributions=(
            DistributionProvenance(name="pandas", status="installed", version="3.0.5"),
            DistributionProvenance(name="skrub", status="installed", version="0.11.dev0"),
        ),
    )

    assert diff_runtime(expected, actual) == (
        "python_version",
        "distributions.pandas",
        "distributions.skrub",
    )


def test_effective_config_hash_is_stable_data_safe_and_semantic(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    def config(root: Path, token: str) -> dict[str, object]:
        return {
            "version": 1,
            "artifact_dir": root / ".parity",
            "fail_fast": False,
            "cases": [
                {
                    "name": "aggregate",
                    "fixture": root / "fixtures" / "input.arrow",
                    "reference": {
                        "target": "study:pandas_aggregate",
                        "record_distributions": ["skrub"],
                        "workdir": root,
                        "environment": {"API_TOKEN": token, "MODE": "study"},
                    },
                    "candidate": {
                        "target": "study:polars_aggregate",
                        "workdir": root,
                    },
                    "comparison": {"dtype": "compatible"},
                },
                {"name": "other", "comparison": {"dtype": "strict"}},
            ],
        }

    first = effective_config_sha256(
        config(first_root, "first-secret"),
        selected_cases={"aggregate"},
        base_directory=first_root,
    )
    moved = effective_config_sha256(
        config(second_root, "different-secret"),
        selected_cases={"aggregate"},
        base_directory=second_root,
    )
    assert first == moved
    assert len(first) == 64
    assert "secret" not in first

    changed = config(second_root, "different-secret")
    cases = changed["cases"]
    assert isinstance(cases, list)
    aggregate = cases[0]
    assert isinstance(aggregate, dict)
    comparison = aggregate["comparison"]
    assert isinstance(comparison, dict)
    comparison["dtype"] = "strict"
    assert (
        effective_config_sha256(
            changed,
            selected_cases={"aggregate"},
            base_directory=second_root,
        )
        != first
    )

    with pytest.raises(ValueError, match="unknown selected"):
        effective_config_sha256(config(first_root, "x"), selected_cases={"missing"})


def test_effective_config_hash_preserves_positional_bundle_order() -> None:
    def contract(names: tuple[str, str]) -> dict[str, object]:
        return {
            "version": 1,
            "cases": [
                {
                    "name": "join",
                    "input_bundle": {
                        "binding": "positional",
                        "inputs": {
                            name: {"schema": {"columns": [{"name": "x", "dtype": "int64"}]}}
                            for name in names
                        },
                    },
                }
            ],
        }

    assert effective_config_sha256(contract(("left", "right"))) != effective_config_sha256(
        contract(("right", "left"))
    )


def test_effective_config_hash_includes_keyed_row_alignment_contract() -> None:
    def contract(row_keys: tuple[str, ...]) -> dict[str, object]:
        return {
            "version": 1,
            "cases": [
                {
                    "name": "orders",
                    "fixture": "input.arrow",
                    "comparison": {
                        "row_order": "keyed",
                        "row_keys": list(row_keys),
                        "dtype": "compatible",
                    },
                }
            ],
        }

    composite = effective_config_sha256(contract(("account", "sequence")))
    assert composite == effective_config_sha256(contract(("account", "sequence")))
    assert composite != effective_config_sha256(contract(("sequence", "account")))
    assert composite != effective_config_sha256(contract(("account",)))


def test_effective_config_hash_does_not_treat_static_inputs_mapping_as_bundle_order() -> None:
    def contract(items: tuple[tuple[str, int], ...]) -> dict[str, object]:
        return {
            "version": 1,
            "cases": [
                {
                    "name": "single",
                    "fixture": "input.arrow",
                    "static_kwargs": {"inputs": dict(items)},
                }
            ],
        }

    assert effective_config_sha256(contract((("z", 1), ("a", 2)))) == effective_config_sha256(
        contract((("a", 2), ("z", 1)))
    )
