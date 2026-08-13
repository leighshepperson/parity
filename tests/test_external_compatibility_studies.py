from __future__ import annotations

import json
import tomllib
from pathlib import Path

from parity.models import ParityConfig

ROOT = Path(__file__).parents[1]
CASE_STUDIES = ROOT / "case_studies"
PYINDICATORS = CASE_STUDIES / "pyindicators_ema"
POLARS_VERSIONS = CASE_STUDIES / "polars_version_dynamic"
PANDAS_VERSIONS = CASE_STUDIES / "pandas_version_groupby"
PYINDICATORS_COMMIT = "9aec2b2caa502301bca6e9937e89e57f8ddeefe1"


def _config(study: Path) -> ParityConfig:
    raw = tomllib.loads((study / "parity.toml").read_text(encoding="utf-8"))
    return ParityConfig.model_validate(raw)


def _report(study: Path) -> dict[str, object]:
    return json.loads((study / "reports" / "report.json").read_text(encoding="utf-8"))


def _installed_versions(case: dict[str, object], endpoint: str) -> dict[str, str]:
    provenance = case["provenance"]
    assert isinstance(provenance, dict)
    runtime = provenance[endpoint]
    assert isinstance(runtime, dict)
    distributions = runtime["distributions"]
    assert isinstance(distributions, list)
    return {
        item["name"]: item["version"]
        for item in distributions
        if isinstance(item, dict) and item.get("status") == "installed"
    }


def _assert_report_uses_current_parity(report: dict[str, object]) -> None:
    assert report["parity_version"] == "0.8.0"
    provenance = report["provenance"]
    assert isinstance(provenance, dict)
    orchestrator = provenance["orchestrator"]
    assert isinstance(orchestrator, dict)
    assert orchestrator["parity_version"] == "0.8.0"
    cases = report["cases"]
    assert isinstance(cases, list)
    for case in cases:
        assert isinstance(case, dict)
        case_provenance = case["provenance"]
        assert isinstance(case_provenance, dict)
        for endpoint in ("reference", "candidate"):
            runtime = case_provenance[endpoint]
            assert isinstance(runtime, dict)
            assert runtime["parity_version"] == "0.8.0"


def test_pyindicators_study_pins_public_source_and_two_backend_cases() -> None:
    config = _config(PYINDICATORS)
    assert [case.name for case in config.cases] == [
        "ema-finite-control",
        "ema-nullable-backend-divergence",
    ]
    for case in config.cases:
        assert case.reference.target == "pyindicators_parity:pandas_ema"
        assert case.reference.adapter == "pandas"
        assert case.reference.pandas_input == "native"
        assert case.candidate.target == "pyindicators_parity:polars_ema"
        assert case.candidate.adapter == "polars"
        assert case.comparison.row_order == "keyed"
        assert case.comparison.row_keys == ["row_id"]
        assert case.generation.max_examples == 1
        assert not case.generation.adversarial_examples
        assert not case.performance.enabled

    requirements = (PYINDICATORS / "environments" / "requirements.txt").read_text()
    for pin in (
        PYINDICATORS_COMMIT,
        "numpy==2.2.5",
        "pandas==2.2.3",
        "pip==26.2.1",
        "polars==1.27.1",
        "pyarrow==23.0.1",
        "scikit-learn==1.9.0",
        "scipy==1.18.0",
    ):
        assert pin in requirements


def test_pyindicators_live_report_has_control_and_nullable_finding() -> None:
    report = _report(PYINDICATORS)
    _assert_report_uses_current_parity(report)
    assert report["schema_version"] == 3
    assert report["status"] == "failed"
    cases = report["cases"]
    assert isinstance(cases, list)
    assert [case["status"] for case in cases] == ["passed", "failed"]
    assert cases[0]["findings_discovered"] == 0
    assert cases[1]["findings_discovered"] == 1
    assert cases[1]["failures"][0]["mismatch_counts"] == {"exception": 1}
    for case in cases:
        for endpoint in ("reference", "candidate"):
            versions = _installed_versions(case, endpoint)
            assert versions["pyindicators"] == "0.21.0"
            assert versions["pandas"] == "2.2.3"
            assert versions["polars"] == "1.27.1"
            assert versions["pyarrow"] == "23.0.1"

    direct = json.loads((PYINDICATORS / "reports" / "direct-repro.json").read_text())
    assert direct["finite"]["pandas"]["outcome"] == "returned"
    assert direct["finite"]["polars"]["outcome"] == "returned"
    assert direct["nullable"]["pandas"]["outcome"] == "returned"
    assert direct["nullable"]["polars"] == {
        "exception_module": "builtins",
        "exception_type": "TypeError",
        "outcome": "raised",
    }


def test_polars_version_study_uses_same_target_in_two_supplied_runtimes() -> None:
    case = _config(POLARS_VERSIONS).cases[0]
    assert case.reference.target == case.candidate.target
    assert case.reference.adapter == case.candidate.adapter == "polars"
    assert case.reference.python == Path("environments/reference/.venv/bin/python")
    assert case.candidate.python == Path("environments/candidate/.venv/bin/python")
    assert case.generation.max_examples == 1
    assert not case.performance.enabled
    assert (
        "polars==0.20.31"
        in (POLARS_VERSIONS / "environments" / "reference" / "requirements.txt").read_text()
    )
    assert (
        "polars==1.41.1"
        in (POLARS_VERSIONS / "environments" / "candidate" / "requirements.txt").read_text()
    )
    for requirements in (
        POLARS_VERSIONS / "environments" / "reference" / "requirements.txt",
        POLARS_VERSIONS / "environments" / "candidate" / "requirements.txt",
    ):
        assert "pip==26.2.1" in requirements.read_text()


def test_polars_version_report_captures_intentional_drift() -> None:
    report = _report(POLARS_VERSIONS)
    _assert_report_uses_current_parity(report)
    assert report["status"] == "failed"
    case = report["cases"][0]
    assert case["status"] == "failed"
    assert case["findings_discovered"] == 1
    counts = case["failures"][0]["mismatch_counts"]
    assert counts["shape"] == 1
    reference = _installed_versions(case, "reference")
    candidate = _installed_versions(case, "candidate")
    assert reference["polars"] == "0.20.31"
    assert candidate["polars"] == "1.41.1"
    for versions in (reference, candidate):
        assert versions["numpy"] == "2.3.5"
        assert versions["pyarrow"] == "23.0.1"

    reference_direct = json.loads(
        (POLARS_VERSIONS / "reports" / "reference-direct-repro.json").read_text()
    )
    candidate_direct = json.loads(
        (POLARS_VERSIONS / "reports" / "candidate-direct-repro.json").read_text()
    )
    assert (reference_direct["polars_version"], reference_direct["row_count"]) == (
        "0.20.31",
        3,
    )
    assert (candidate_direct["polars_version"], candidate_direct["row_count"]) == (
        "1.41.1",
        2,
    )


def test_pandas_version_study_uses_current_non_yanked_candidate() -> None:
    case = _config(PANDAS_VERSIONS).cases[0]
    assert case.reference.target == case.candidate.target
    assert case.reference.adapter == case.candidate.adapter == "pandas"
    assert case.reference.python == Path("environments/reference/.venv/bin/python")
    assert case.candidate.python == Path("environments/candidate/.venv/bin/python")
    reference_requirements = (
        PANDAS_VERSIONS / "environments" / "reference" / "requirements.txt"
    ).read_text()
    candidate_requirements = (
        PANDAS_VERSIONS / "environments" / "candidate" / "requirements.txt"
    ).read_text()
    assert "pandas==2.3.3" in reference_requirements
    assert "pandas==3.0.5" in candidate_requirements
    assert "pandas==3.0.4" not in candidate_requirements
    assert "pip==26.2.1" in reference_requirements
    assert "pip==26.2.1" in candidate_requirements
    assert "pyarrow==23.0.1" in reference_requirements
    assert "pyarrow==23.0.1" in candidate_requirements


def test_pandas_version_report_captures_observed_default_drift() -> None:
    report = _report(PANDAS_VERSIONS)
    _assert_report_uses_current_parity(report)
    assert report["status"] == "failed"
    case = report["cases"][0]
    assert case["status"] == "failed"
    assert case["findings_discovered"] == 1
    assert case["failures"][0]["mismatch_counts"] == {"shape": 1}
    reference = _installed_versions(case, "reference")
    candidate = _installed_versions(case, "candidate")
    assert reference["pandas"] == "2.3.3"
    assert candidate["pandas"] == "3.0.5"
    for versions in (reference, candidate):
        assert versions["numpy"] == "2.3.5"
        assert versions["pyarrow"] == "23.0.1"

    reference_direct = json.loads(
        (PANDAS_VERSIONS / "reports" / "reference-direct-repro.json").read_text()
    )
    candidate_direct = json.loads(
        (PANDAS_VERSIONS / "reports" / "candidate-direct-repro.json").read_text()
    )
    assert (reference_direct["pandas_version"], reference_direct["row_count"]) == ("2.3.3", 2)
    assert (candidate_direct["pandas_version"], candidate_direct["row_count"]) == ("3.0.5", 1)


def test_new_studies_are_static_data_safe_and_ignore_generated_state() -> None:
    for study, private_marker in (
        (PYINDICATORS, "10.0"),
        (POLARS_VERSIONS, "2024-01-01T00:00:00"),
        (PANDAS_VERSIONS, "unused"),
    ):
        gitignore = (study / ".gitignore").read_text()
        assert ".parity-" in gitignore
        if study != PYINDICATORS:
            assert "environments/*/.venv/" in gitignore
        report_text = (study / "reports" / "report.json").read_text()
        markdown = (study / "reports" / "report.md").read_text()
        assert private_marker not in report_text
        assert private_marker not in markdown
        assert "Compared row values are omitted" in markdown
        for source_name in (
            name
            for name in (
                "pyindicators_parity.py",
                "polars_version_parity.py",
                "pandas_version_parity.py",
            )
            if (study / name).exists()
        ):
            source_path = study / source_name
            compile(source_path.read_text(), str(source_path), "exec")
