from __future__ import annotations

import ast
import json
import tomllib
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

from parity.config import load_config
from parity.migration import load_migration_manifest, migration_manifest_sha256
from parity.migration_workspace import MigrationWorkspace
from parity.models import ParityConfig

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "pytimetk_migration"

CASE_NAMES = [
    "lags-control",
    "lags-grouped-unsorted",
    "lags-null-semantics",
    "rolling-control",
    "rolling-grouped-centered",
    "rolling-null-semantics",
    "ewm-control",
    "ewm-grouped-unsorted",
    "ewm-null-semantics",
    "macd-control",
    "macd-grouped-unsorted",
    "macd-null-semantics",
    "pad-control",
    "pad-grouped-bounds",
    "pad-null-semantics",
]

COVERED_UNITS = {
    "augment-lags.cpu-core": CASE_NAMES[0:3],
    "augment-rolling.cpu-builtins": CASE_NAMES[3:6],
    "augment-ewm.cpu-native": CASE_NAMES[6:9],
    "augment-macd.cpu-core": CASE_NAMES[9:12],
    "pad-by-time.cpu-fixed-frequency": CASE_NAMES[12:15],
}

EXCLUDED_UNITS = {
    "cross-cutting.cudf",
    "cross-cutting.reduce-memory",
    "augment-lags.selectors-duration",
    "augment-rolling.custom-parallel",
    "augment-ewm.pandas-fallbacks",
    "pad-by-time.calendar-timezone-selectors",
}

TARGETS = {
    "lags-control": ("lags_control_pandas", "lags_control_polars"),
    "lags-grouped-unsorted": ("lags_grouped_pandas", "lags_grouped_polars"),
    "lags-null-semantics": ("lags_null_pandas", "lags_null_polars"),
    "rolling-control": ("rolling_control_pandas", "rolling_control_polars"),
    "rolling-grouped-centered": ("rolling_grouped_pandas", "rolling_grouped_polars"),
    "rolling-null-semantics": ("rolling_null_pandas", "rolling_null_polars"),
    "ewm-control": ("ewm_control_pandas", "ewm_control_polars"),
    "ewm-grouped-unsorted": ("ewm_grouped_pandas", "ewm_grouped_polars"),
    "ewm-null-semantics": ("ewm_null_pandas", "ewm_null_polars"),
    "macd-control": ("macd_control_pandas", "macd_control_polars"),
    "macd-grouped-unsorted": ("macd_grouped_pandas", "macd_grouped_polars"),
    "macd-null-semantics": ("macd_null_pandas", "macd_null_polars"),
    "pad-control": ("pad_control_pandas", "pad_control_polars"),
    "pad-grouped-bounds": ("pad_grouped_pandas", "pad_grouped_polars"),
    "pad-null-semantics": ("pad_null_pandas", "pad_null_polars"),
}


def _config(name: str) -> ParityConfig:
    raw = tomllib.loads((STUDY / name).read_text(encoding="utf-8"))
    return ParityConfig.model_validate(raw)


def _arrow_table(name: str) -> pa.Table:
    with pa.memory_map(str(STUDY / "fixtures" / name), "r") as source:
        return ipc.open_file(source).read_all()


def _report(*parts: str) -> dict[str, object]:
    return json.loads((STUDY / "reports" / Path(*parts)).read_text(encoding="utf-8"))


def _distribution_version(case: dict[str, object], side: str, name: str) -> str:
    provenance = case["provenance"]
    assert isinstance(provenance, dict)
    endpoint = provenance[side]
    assert isinstance(endpoint, dict)
    distributions = endpoint["distributions"]
    assert isinstance(distributions, list)
    for distribution in distributions:
        assert isinstance(distribution, dict)
        if distribution["name"] == name:
            version = distribution["version"]
            assert isinstance(version, str)
            return version
    raise AssertionError(f"missing {name!r} provenance on {side}")


def test_migration_manifest_is_complete_for_the_declared_scope() -> None:
    manifest = load_migration_manifest(STUDY / "migration.toml")
    units = {unit.id: unit for unit in manifest.units}

    assert len(units) == 11
    assert set(units) == set(COVERED_UNITS) | EXCLUDED_UNITS
    for unit_id, cases in COVERED_UNITS.items():
        assert units[unit_id].cases == cases
        assert units[unit_id].excluded_reason is None
    for unit_id in EXCLUDED_UNITS:
        assert units[unit_id].cases == []
        assert units[unit_id].excluded_reason

    mapped = {name for cases in COVERED_UNITS.values() for name in cases}
    assert mapped == set(CASE_NAMES)


def test_release_and_current_configs_have_the_same_strict_campaigns() -> None:
    for lane in ("release", "current"):
        config = _config(f"parity.{lane}.toml")
        assert [case.name for case in config.cases] == CASE_NAMES
        assert not config.fail_fast

        for case in config.cases:
            reference_name, candidate_name = TARGETS[case.name]
            assert case.reference.target == f"pytimetk_pilot:{reference_name}"
            assert case.reference.adapter == "pandas"
            assert case.reference.pandas_input == "native"
            assert case.reference.python == Path(f"environments/{lane}/reference/.venv/bin/python")
            assert case.candidate.target == f"pytimetk_pilot:{candidate_name}"
            assert case.candidate.adapter == "polars"
            assert case.candidate.python == Path(f"environments/{lane}/candidate/.venv/bin/python")
            assert case.reference.record_distributions == ["pytimetk"]
            assert case.candidate.record_distributions == ["pytimetk"]
            assert case.comparison.row_order == "strict"
            assert case.comparison.column_order == "strict"
            assert case.comparison.dtype == "compatible"
            assert case.comparison.null_nan_equal
            assert case.comparison.rtol == 1e-9
            assert case.comparison.atol == 1e-10
            assert case.generation.stability_repeats == 2
            assert not case.performance.enabled

            if case.name.endswith("control") and case.name != "pad-control":
                assert case.schema is not None
                assert case.generation.max_examples == 100
                assert case.generation.search
                assert case.generation.adversarial_examples
                assert case.generation.shrink
            else:
                assert case.fixture is not None
                assert case.generation.max_examples == 1
                assert not case.generation.search
                assert not case.generation.adversarial_examples
                assert not case.generation.shrink


def test_managed_config_expands_the_same_worker_path_free_campaigns() -> None:
    managed = load_config(STUDY / "parity.workspace-config.toml")
    current = load_config(STUDY / "parity.current.toml").model_copy(deep=True)

    assert [case.name for case in managed.cases] == CASE_NAMES
    assert len(managed.cases) == 15
    assert not managed.fail_fast
    assert managed.artifact_dir == (STUDY / ".parity/workspace/artifacts").resolve()

    for case in current.cases:
        case.reference.python = None
        case.candidate.python = None
        case.reference.required_distributions = {"pytimetk": "==2.5.1"}
        case.candidate.required_distributions = {"pytimetk": "==2.5.1+parity.1"}

    assert managed.cases == current.cases
    for case in managed.cases:
        assert case.reference.python is None
        assert case.candidate.python is None
        assert case.reference.record_distributions == ["pytimetk"]
        assert case.candidate.record_distributions == ["pytimetk"]
        assert case.reference.required_distributions == {"pytimetk": "==2.5.1"}
        assert case.candidate.required_distributions == {"pytimetk": "==2.5.1+parity.1"}


def test_managed_workspace_declares_one_checkout_and_two_reviewed_lanes() -> None:
    raw = tomllib.loads((STUDY / "parity.workspace.toml").read_text(encoding="utf-8"))
    workspace = MigrationWorkspace.model_validate(raw)

    assert workspace.reference == "pytimetk==2.5.1"
    assert workspace.candidate == Path("candidate-src/pytimetk")
    assert workspace.python == "3.12"
    assert workspace.config == Path("parity.workspace-config.toml")
    assert workspace.manifest == Path("migration.toml")
    assert workspace.report_dir == Path(".parity/workspace/reports")
    assert [(lane.name, lane.requirements) for lane in workspace.lanes] == [
        ("release", Path("environments/release/workspace.in")),
        ("current", Path("environments/current/workspace.in")),
    ]

    reviewed_pins = {
        "release": {
            "numpy==2.0.2",
            "pandas==2.2.3",
            "pandas-flavor==0.7.0",
            "polars==1.21.0",
            "pyarrow==16.1.0",
            "tqdm==4.67.1",
        },
        "current": {
            "numpy==2.5.2",
            "pandas==3.0.5",
            "pandas-flavor==0.8.1",
            "polars==1.43.2",
            "pyarrow==25.0.1",
            "tqdm==4.70.0",
        },
    }
    for lane in workspace.lanes:
        assert lane.requirements is not None
        lines = {
            line.strip()
            for line in (STUDY / lane.requirements).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert lines == reviewed_pins[lane.name]
        assert not any(line.startswith(("parity-check", "pytimetk")) for line in lines)


def test_lane_configs_differ_only_in_runtime_paths_and_artifact_directory() -> None:
    release = _config("parity.release.toml").model_dump(mode="json")
    current = _config("parity.current.toml").model_dump(mode="json")

    release["artifact_dir"] = "<artifact>"
    current["artifact_dir"] = "<artifact>"
    for payload in (release, current):
        for case in payload["cases"]:
            for endpoint in ("reference", "candidate"):
                implementation = case[endpoint]
                implementation["python"] = (
                    str(implementation["python"])
                    .replace("/release/", "/<lane>/")
                    .replace("/current/", "/<lane>/")
                )

    assert release == current


def test_version_drift_config_compares_identical_callables_across_lanes() -> None:
    config = _config("parity.version-drift.toml")
    assert len(config.cases) == 10
    assert {case.name for case in config.cases} == {
        f"{backend}-{api}-drift"
        for backend in ("pandas", "polars")
        for api in ("lags", "rolling", "ewm", "macd", "pad")
    }

    for case in config.cases:
        assert case.reference.target == case.candidate.target
        assert case.comparison.row_order == "strict"
        assert case.comparison.column_order == "strict"
        assert case.comparison.null_nan_equal
        assert case.comparison.rtol == 1e-9
        assert case.comparison.atol == 1e-10
        assert case.generation.max_examples == 1
        assert not case.generation.search
        assert not case.generation.adversarial_examples
        assert not case.generation.shrink
        assert not case.performance.enabled

        if case.name.startswith("pandas-"):
            assert case.reference.adapter == case.candidate.adapter == "pandas"
            assert case.reference.pandas_input == case.candidate.pandas_input == "native"
            assert case.reference.python == Path("environments/release/reference/.venv/bin/python")
            assert case.candidate.python == Path("environments/current/reference/.venv/bin/python")
        else:
            assert case.reference.adapter == case.candidate.adapter == "polars"
            assert case.reference.python == Path("environments/release/candidate/.venv/bin/python")
            assert case.candidate.python == Path("environments/current/candidate/.venv/bin/python")


def test_public_wrapper_module_defines_every_configured_target() -> None:
    source = (STUDY / "pytimetk_pilot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    functions = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
    expected = {target for pair in TARGETS.values() for target in pair}
    assert expected <= functions


def test_synthetic_arrow_fixtures_preserve_hostile_structure() -> None:
    unsorted = _arrow_table("augment_unsorted.arrow")
    assert unsorted.column_names == ["row_id", "group", "date", "value", "volume"]
    assert unsorted.num_rows == 8
    assert unsorted["date"].to_pylist()[:3] == [
        datetime(2024, 1, 3),
        datetime(2024, 1, 2),
        datetime(2024, 1, 1),
    ]

    hostile = _arrow_table("augment_hostile.arrow")
    assert hostile.num_rows == 10
    assert hostile["group"].null_count == 2
    assert hostile["date"].null_count == 2
    assert hostile["value"].null_count == 2

    pad = _arrow_table("pad.arrow")
    assert pad.column_names == ["series", "date", "value", "constant"]
    assert pad.num_rows == 3

    pad_hostile = _arrow_table("pad_numeric_hostile.arrow")
    assert pad_hostile["group"].null_count == 2
    assert pad_hostile["date"].null_count == 2


def test_environment_inputs_pin_the_reviewed_dependency_lanes() -> None:
    expected = {
        "release": (
            "parity-check==0.9.2",
            "pytimetk==2.5.1",
            "numpy==2.0.2",
            "pandas==2.2.3",
            "pandas-flavor==0.7.0",
            "polars==1.21.0",
            "pyarrow==16.1.0",
        ),
        "current": (
            "parity-check==0.9.2",
            "pytimetk==2.5.1",
            "numpy==2.5.2",
            "pandas==3.0.5",
            "pandas-flavor==0.8.1",
            "polars==1.43.2",
            "pyarrow==25.0.1",
        ),
    }
    for lane, pins in expected.items():
        requirements = (STUDY / "environments" / lane / "requirements.in").read_text()
        for pin in pins:
            assert pin in requirements


def test_captured_migration_reports_bind_stock_failures_and_repaired_passes() -> None:
    manifest_hash = migration_manifest_sha256(load_migration_manifest(STUDY / "migration.toml"))
    historical_config_hashes = {
        "release": "821aef184819c570861ba3c6afbfbde18e7302083b97225430076834f99cd720",
        "current": "77b1966a6bc09ef38e10046cef6c1b3f335fe749941ef28af51228657daea634",
    }
    failed_summary = {
        "total": 11,
        "passed": 0,
        "failed": 5,
        "error": 0,
        "excluded": 6,
        "uncovered": 0,
    }
    passed_summary = {
        "total": 11,
        "passed": 5,
        "failed": 0,
        "error": 0,
        "excluded": 6,
        "uncovered": 0,
    }

    for lane in ("release", "current"):
        baseline = _report("baseline", lane, "migration.json")
        repaired = _report("final", lane, "migration.json")

        assert baseline["status"] == "failed"
        assert baseline["summary"] == failed_summary
        assert repaired["status"] == "passed"
        assert repaired["summary"] == passed_summary
        assert baseline["manifest_sha256"] == repaired["manifest_sha256"] == manifest_hash

        for report in (baseline, repaired):
            parity = report["parity"]
            assert isinstance(parity, dict)
            assert parity["parity_version"] == "0.9.2"
            provenance = parity["provenance"]
            assert isinstance(provenance, dict)
            # v0.10 adds effective contract fields, so recomputing with the current model
            # intentionally produces a different digest from these immutable v0.9.2 reports.
            assert provenance["config_sha256"] == historical_config_hashes[lane]

        baseline_parity = baseline["parity"]
        repaired_parity = repaired["parity"]
        assert isinstance(baseline_parity, dict)
        assert isinstance(repaired_parity, dict)
        baseline_cases = baseline_parity["cases"]
        repaired_cases = repaired_parity["cases"]
        assert isinstance(baseline_cases, list)
        assert isinstance(repaired_cases, list)
        assert [case["name"] for case in baseline_cases] == CASE_NAMES
        assert [case["name"] for case in repaired_cases] == CASE_NAMES

        expected_failed_cases = {
            "lags-null-semantics",
            "rolling-null-semantics",
            "ewm-grouped-unsorted",
            "ewm-null-semantics",
            "macd-null-semantics",
            "pad-control",
            "pad-null-semantics",
        }
        if lane == "release":
            expected_failed_cases.add("ewm-control")
        assert {
            case["name"] for case in baseline_cases if case["status"] == "failed"
        } == expected_failed_cases

        baseline_failures = [failure for case in baseline_cases for failure in case["failures"]]
        assert baseline_failures
        assert all(
            failure["artifact"].startswith(f".parity-{lane}/") for failure in baseline_failures
        )
        assert all(
            _distribution_version(case, "reference", "pytimetk") == "2.5.1"
            and _distribution_version(case, "candidate", "pytimetk") == "2.5.1"
            for case in baseline_cases
        )

        for case in repaired_cases:
            assert case["status"] == "passed"
            assert case["failures"] == []
            assert _distribution_version(case, "reference", "pytimetk") == "2.5.1"
            assert _distribution_version(case, "candidate", "pytimetk") == "2.5.1+parity.1"
            if case["name"].endswith("control") and case["name"] != "pad-control":
                assert case["deterministic_examples"] == 0
                assert case["generated_examples"] == 100
            else:
                assert case["deterministic_examples"] == 1
                assert case["generated_examples"] == 0


def test_captured_version_drift_report_passes_both_backend_axes() -> None:
    report = _report("version-drift", "report.json")
    assert report["status"] == "passed"
    assert report["parity_version"] == "0.9.2"
    provenance = report["provenance"]
    assert isinstance(provenance, dict)
    assert (
        provenance["config_sha256"]
        == "af09e2076537fc47903ed822637cd34326608f0f3ef42a4ebaa33758ad3b5292"
    )

    cases = report["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 10
    for case in cases:
        assert case["status"] == "passed"
        assert case["deterministic_examples"] == 1
        assert case["generated_examples"] == 0
        assert case["failures"] == []
        expected_source = "2.5.1" if case["name"].startswith("pandas-") else "2.5.1+parity.1"
        assert _distribution_version(case, "reference", "pytimetk") == expected_source
        assert _distribution_version(case, "candidate", "pytimetk") == expected_source

    first = cases[0]
    assert _distribution_version(first, "reference", "pandas") == "2.2.3"
    assert _distribution_version(first, "candidate", "pandas") == "3.0.5"
    assert _distribution_version(first, "reference", "polars") == "1.21.0"
    assert _distribution_version(first, "candidate", "polars") == "1.43.2"


def test_committed_reports_are_data_safe() -> None:
    report_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((STUDY / "reports").rglob("*"))
        if path.is_file()
    )
    assert str(ROOT) not in report_text
    assert "/tmp/" not in report_text
    assert "2024-01-01" not in report_text
