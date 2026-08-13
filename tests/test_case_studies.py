from __future__ import annotations

import json
import tomllib
from pathlib import Path

from parity.models import ParityConfig

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "pyjanitor_complete"
SKRUB_STUDY = ROOT / "case_studies" / "skrub_agg_joiner"
SKRUB_COMMIT = "55dc7f45e140ccb76e768e3e4b4193f4eac3d5aa"

SKRUB_CASES = [
    "aggregate-numeric-control",
    "aggjoiner-numeric-control",
    "aggregate-unique-mode-control",
    "aggregate-arrow-null-control",
    "aggregate-null-key-finding",
    "aggjoiner-tied-mode-finding",
    "aggjoiner-ieee-nan-finding",
]


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


def test_skrub_case_study_config_and_reports_are_consistent() -> None:
    raw = tomllib.loads((SKRUB_STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)

    assert [case.name for case in config.cases] == SKRUB_CASES
    assert all(case.generation.max_examples == 1 for case in config.cases)
    assert all(not case.generation.adversarial_examples for case in config.cases)
    assert all(not case.performance.enabled for case in config.cases)
    for case in config.cases:
        assert case.fixture is not None
        assert (SKRUB_STUDY / case.fixture).is_file()
        for endpoint in (case.reference, case.candidate):
            assert endpoint.record_distributions == ["scikit-learn", "skrub"]
    native = [
        case.reference.pandas_input for case in config.cases if case.reference.adapter == "pandas"
    ]
    assert native.count("native") == 6
    assert native.count("arrow") == 1

    for lane in ("floor", "current"):
        report = json.loads(
            (SKRUB_STUDY / "reports" / lane / "report.json").read_text(encoding="utf-8")
        )
        assert report["schema_version"] == 2
        assert report["parity_version"] == "0.2.0"
        assert [case["name"] for case in report["cases"]] == SKRUB_CASES
        assert [case["status"] for case in report["cases"]] == [
            "passed",
            "passed",
            "passed",
            "passed",
            "failed",
            "failed",
            "failed",
        ]
        assert all(case["provenance"]["verification"] == "captured" for case in report["cases"])
        assert all(
            {item["name"] for item in case["provenance"]["reference"]["distributions"]}
            >= {"skrub", "scikit-learn"}
            for case in report["cases"]
        )


def test_skrub_case_study_targets_locks_and_fixture_hashes_are_pinned() -> None:
    raw = tomllib.loads((SKRUB_STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    targets = {
        endpoint.target.partition(":")[2]
        for case in config.cases
        for endpoint in (case.reference, case.candidate)
    }
    source = (SKRUB_STUDY / "skrub_parity.py").read_text(encoding="utf-8")
    compile(source, str(SKRUB_STUDY / "skrub_parity.py"), "exec")
    for target in targets:
        assert f"def {target}(" in source

    expected_hashes = {
        "arrow_null.arrow": "968ed9b889474a812aee929142c0ff33fdd82c82f0e04efde9cf42316aa9381d",
        "ieee_nan.arrow": "b43a7f1e2c75e059f77c9b84a9c49f531d41d686884014856cfc6f3f2131b90c",
    }
    import hashlib

    for name, expected in expected_hashes.items():
        assert (
            hashlib.sha256((SKRUB_STUDY / "fixtures" / name).read_bytes()).hexdigest() == expected
        )

    floor = (SKRUB_STUDY / "environments" / "floor" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    current = (SKRUB_STUDY / "environments" / "current" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert SKRUB_COMMIT in floor
    assert SKRUB_COMMIT in current
    for pin in (
        "pandas==2.1.0",
        "polars==1.5.0",
        "pyarrow==16.0.0",
        "psutil==5.9.8",
        "typer==0.16.1",
        "click==8.2.1",
    ):
        assert pin in floor
    for pin in (
        "pandas==3.0.5",
        "polars==1.43.2",
        "pyarrow==25.0.1",
        "numpy==2.5.2",
    ):
        assert pin in current


def test_skrub_case_study_is_data_safe_and_has_only_draft_upstream_issues() -> None:
    readme = (SKRUB_STUDY / "README.md").read_text(encoding="utf-8")
    drafts = (SKRUB_STUDY / "UPSTREAM_ISSUES.md").read_text(encoding="utf-8")
    ignore = (SKRUB_STUDY / ".gitignore").read_text(encoding="utf-8")
    assert SKRUB_COMMIT in readme
    assert "Parity 0.2.0" in readme
    assert "synthetic" in readme
    assert "Nothing has been filed upstream" in drafts
    assert drafts.count(SKRUB_COMMIT) == 1
    assert ".parity-skrub/" in ignore
    assert not list(SKRUB_STUDY.glob(".parity-skrub"))
