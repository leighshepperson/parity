from __future__ import annotations

import json
import tomllib
from pathlib import Path

from parity.models import ParityConfig

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "utilsforecast_evaluate"
UTILSFORECAST_COMMIT = "ce2c7ddc7b71228ece21edf72ef9567d7467c0ab"


def test_utilsforecast_study_has_a_pinned_keyed_control() -> None:
    raw = tomllib.loads((STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    assert len(config.cases) == 1

    case = config.cases[0]
    assert case.name == "evaluate-mae-rmse"
    assert case.fixture == Path("fixtures/forecast.json")
    assert case.reference.adapter == "pandas"
    assert case.reference.pandas_input == "native"
    assert case.candidate.adapter == "polars"
    assert case.comparison.row_order == "keyed"
    assert case.comparison.row_keys == ["unique_id", "metric"]
    assert case.comparison.rtol == 1e-12
    assert case.comparison.atol == 1e-12
    assert case.generation.max_examples == 1
    assert not case.generation.adversarial_examples
    assert not case.generation.shrink
    assert not case.performance.enabled


def test_utilsforecast_study_is_static_without_the_optional_dependency() -> None:
    fixture = json.loads((STUDY / "fixtures" / "forecast.json").read_text(encoding="utf-8"))
    assert len(fixture) == 6
    assert {(row["unique_id"], row["ds"]) for row in fixture} == {
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
        (3, 1),
        (3, 2),
    }
    assert all(isinstance(row["y"], float) for row in fixture)
    assert all(isinstance(row["model"], float) for row in fixture)

    source_path = STUDY / "utilsforecast_parity.py"
    source = source_path.read_text(encoding="utf-8")
    compile(source, str(source_path), "exec")
    assert "def pandas_evaluate(" in source
    assert "def polars_evaluate(" in source
    assert 'metrics=[mae, rmse], models=["model"]' in source


def test_utilsforecast_study_reproduction_is_exact_and_data_safe() -> None:
    requirements = (STUDY / "environments" / "requirements.txt").read_text(encoding="utf-8")
    for pin in (
        UTILSFORECAST_COMMIT,
        "narwhals==2.15.0",
        "numpy==2.3.5",
        "pandas==2.3.3",
        "polars==1.31.0",
        "pyarrow==23.0.0",
    ):
        assert pin in requirements

    readme = (STUDY / "README.md").read_text(encoding="utf-8")
    assert UTILSFORECAST_COMMIT in readme
    assert "six-row synthetic" in readme
    assert "exits `0`" in readme
    assert ".parity-utilsforecast/" in (STUDY / ".gitignore").read_text(encoding="utf-8")


def test_utilsforecast_live_report_is_a_clean_pinned_pass() -> None:
    report = json.loads((STUDY / "reports" / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 3
    assert report["parity_version"] == "0.7.0"
    assert report["status"] == "passed"
    assert len(report["cases"]) == 1

    case = report["cases"][0]
    assert case["name"] == "evaluate-mae-rmse"
    assert case["status"] == "passed"
    assert case["failures"] == []
    assert case["findings_discovered"] == 0
    assert case["provenance"]["verification"] == "captured"
    for endpoint in ("reference", "candidate"):
        versions = {
            item["name"]: item["version"]
            for item in case["provenance"][endpoint]["distributions"]
            if item["status"] == "installed"
        }
        expected = {
            "narwhals": "2.15.0",
            "numpy": "2.3.5",
            "pandas": "2.3.3",
            "polars": "1.31.0",
            "pyarrow": "23.0.0",
            "utilsforecast": "0.2.16",
        }
        assert all(versions[name] == version for name, version in expected.items())

    markdown = (STUDY / "reports" / "report.md").read_text(encoding="utf-8")
    assert "**PASSED**" in markdown
    assert "Compared row values are omitted" in markdown
