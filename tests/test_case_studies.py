from __future__ import annotations

import json
import tomllib
from pathlib import Path

from parity.models import ParityConfig

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "pyjanitor_complete"


def test_pyjanitor_case_study_config_and_evidence_are_consistent() -> None:
    raw = tomllib.loads((STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    report = json.loads((STUDY / "report.json").read_text(encoding="utf-8"))

    case_names = [case.name for case in config.cases]
    report_names = [case["name"] for case in report["cases"]]
    assert len(case_names) == 16
    assert case_names == report_names
    assert sum(case["examples_run"] for case in report["cases"]) == 1_643
    assert sum(case["status"] == "passed" for case in report["cases"]) == 8
    assert sum(case["status"] == "failed" for case in report["cases"]) == 8

    for case in config.cases:
        if case.fixture is not None:
            assert (STUDY / case.fixture).is_file()


def test_pyjanitor_case_study_targets_exist_without_importing_optional_dependency() -> None:
    raw = tomllib.loads((STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    targets = {
        endpoint.target.partition(":")[2]
        for case in config.cases
        for endpoint in (case.reference, case.candidate)
    }

    # Importing the module would require the opt-in pyjanitor dependency. Source
    # compilation plus its declared public function names keeps normal CI isolated.
    source = (STUDY / "pyjanitor_parity.py").read_text(encoding="utf-8")
    compile(source, str(STUDY / "pyjanitor_parity.py"), "exec")
    for target in targets:
        assert f"def {target}(" in source


def test_pyjanitor_case_study_readme_has_pinned_reproduction() -> None:
    content = (STUDY / "README.md").read_text(encoding="utf-8")
    assert "c1b57b993dca4348e9acc41301fe8526dcae57df" in content
    assert "parity-check==0.1.0" in content
    assert "pandas==3.0.5" in content
    assert "polars==1.43.2" in content
    assert "pyarrow==25.0.1" in content
    assert "expected" not in content.lower() or "exits `1`" in content


def test_pyjanitor_issue_drafts_include_standalone_reproductions() -> None:
    content = (STUDY / "UPSTREAM_ISSUES.md").read_text(encoding="utf-8")
    assert "loses payload values when completion keys contain nulls" in content
    assert "drops existing rows outside an explicit domain" in content
    assert content.count("import janitor.polars") == 2
    assert content.count("c1b57b993dca4348e9acc41301fe8526dcae57df") == 2
