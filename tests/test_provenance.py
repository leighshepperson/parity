from __future__ import annotations

import importlib.metadata
import sys
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
    distribution_satisfies_requirement,
    effective_config_sha256,
    normalize_distribution_name,
    normalize_distribution_names,
    normalize_distribution_requirements,
    runtime_contract_failures,
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
    assert __version__ == source_version == "0.20.0"


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


def test_distribution_requirements_are_pep440_and_fail_closed() -> None:
    assert normalize_distribution_requirements({"NumPy": ">=2, <3"}) == {"numpy": "<3,>=2"}
    assert distribution_satisfies_requirement("2.5.1+local.1", ">=2.5,<3")
    assert not distribution_satisfies_requirement("not-a-version", ">=2.5,<3")
    runtime = _runtime(
        distributions=(
            DistributionProvenance(name="numpy", status="installed", version="2.5.1"),
            DistributionProvenance(name="pandas", status="missing"),
        )
    )

    assert runtime_contract_failures(
        runtime,
        expected_parity_version="0.9.2",
        required_distributions={"numpy": ">=3", "pandas": ">=2", "polars": ">=1"},
    ) == (
        "parity_version",
        "distributions.numpy.version",
        "distributions.pandas.missing",
        "distributions.polars.unavailable",
    )


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
            "version": 2,
            "artifact_dir": root / ".parity",
            "fail_fast": False,
            "cases": [
                {
                    "name": "aggregate",
                    "invocation": {
                        "args": [
                            {
                                "kind": "frame",
                                "fixture": root / "fixtures" / "input.arrow",
                            }
                        ]
                    },
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


def test_effective_config_hash_preserves_project_python_entrypoint_identity(
    tmp_path: Path,
) -> None:
    def config(root: Path, reference_name: str, candidate_name: str) -> dict[str, object]:
        for name in {reference_name, candidate_name}:
            interpreter = root / name / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            if not interpreter.exists():
                interpreter.symlink_to(sys.executable)
        return {
            "version": 2,
            "cases": [
                {
                    "name": "versions",
                    "invocation": {"args": [{"kind": "frame", "fixture": root / "fixture.arrow"}]},
                    "reference": {
                        "target": "project.transform:run",
                        "python": root / reference_name / "bin" / "python",
                        "workdir": root,
                    },
                    "candidate": {
                        "target": "project.transform:run",
                        "python": root / candidate_name / "bin" / "python",
                        "workdir": root,
                    },
                }
            ],
        }

    first_root = tmp_path / "first"
    moved_root = tmp_path / "moved"
    distinct = effective_config_sha256(config(first_root, ".venv-old", ".venv-new"))
    swapped = effective_config_sha256(config(first_root, ".venv-new", ".venv-old"))
    same = effective_config_sha256(config(first_root, ".venv-old", ".venv-old"))
    moved = effective_config_sha256(config(moved_root, ".venv-old", ".venv-new"))

    assert distinct != swapped
    assert distinct != same
    assert distinct == moved


def test_effective_config_hash_omits_external_python_path_identity(tmp_path: Path) -> None:
    def contract(python: Path) -> dict[str, object]:
        return {
            "version": 2,
            "cases": [
                {
                    "name": "external",
                    "invocation": {"args": []},
                    "reference": {
                        "target": "project:run",
                        "python": python,
                        "workdir": tmp_path,
                    },
                    "candidate": {"target": "project:run", "workdir": tmp_path},
                }
            ],
        }

    assert effective_config_sha256(contract(Path("/private/one/python"))) == (
        effective_config_sha256(contract(Path("/different/private/python")))
    )


def test_effective_config_hash_uses_side_workdir_for_python_identity(tmp_path: Path) -> None:
    def contract(root: Path, python_name: str) -> dict[str, object]:
        workdir = root / "worker"
        return {
            "version": 2,
            "cases": [
                {
                    "name": "worker",
                    "invocation": {"args": []},
                    "reference": {
                        "target": "project:run",
                        "python": workdir / python_name / "bin" / "python",
                        "workdir": workdir,
                    },
                    "candidate": {
                        "target": "project:run",
                        "workdir": root / "different-worker",
                    },
                }
            ],
        }

    first = effective_config_sha256(contract(tmp_path / "first", ".venv-a"))
    changed = effective_config_sha256(contract(tmp_path / "first", ".venv-b"))
    moved = effective_config_sha256(contract(tmp_path / "moved", ".venv-a"))

    assert first != changed
    assert first == moved


def test_effective_config_hash_preserves_positional_bundle_order() -> None:
    def contract(names: tuple[str, str]) -> dict[str, object]:
        return {
            "version": 2,
            "cases": [
                {
                    "name": "join",
                    "invocation": {
                        "args": [
                            {
                                "kind": "frame",
                                "name": name,
                                "schema": {"columns": [{"name": "x", "dtype": "int64"}]},
                            }
                            for name in names
                        ]
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
            "version": 2,
            "cases": [
                {
                    "name": "orders",
                    "invocation": {"args": [{"kind": "frame", "fixture": "input.arrow"}]},
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


def test_effective_config_hash_includes_generated_search_policy() -> None:
    def contract(search: bool) -> dict[str, object]:
        return {
            "version": 2,
            "cases": [
                {
                    "name": "fixture-only",
                    "invocation": {"args": [{"kind": "frame", "fixture": "input.arrow"}]},
                    "generation": {"search": search},
                }
            ],
        }

    assert effective_config_sha256(contract(True)) != effective_config_sha256(contract(False))


def test_effective_config_hash_canonicalizes_json_mapping_order() -> None:
    def contract(items: tuple[tuple[str, int], ...]) -> dict[str, object]:
        return {
            "version": 2,
            "cases": [
                {
                    "name": "single",
                    "invocation": {
                        "args": [{"kind": "frame", "fixture": "input.arrow"}],
                        "kwargs": {"inputs": {"kind": "json", "values": [dict(items)]}},
                    },
                }
            ],
        }

    assert effective_config_sha256(contract((("z", 1), ("a", 2)))) == effective_config_sha256(
        contract((("a", 2), ("z", 1)))
    )


def test_effective_config_hash_includes_shared_json_kwargs() -> None:
    def contract(engine: str) -> dict[str, object]:
        return {
            "version": 2,
            "cases": [
                {
                    "name": "engines",
                    "invocation": {
                        "args": [{"kind": "frame", "fixture": "input.arrow"}],
                        "kwargs": {"engine": {"kind": "json", "values": [engine]}},
                    },
                }
            ],
        }

    pandas = effective_config_sha256(contract("pandas"))
    assert pandas == effective_config_sha256(contract("pandas"))
    assert pandas != effective_config_sha256(contract("polars"))
