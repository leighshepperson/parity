from __future__ import annotations

import json
import runpy
import tomllib
from pathlib import Path

import pandas as pd
import polars as pl
import pyarrow as pa

from parity.models import FrameArgument, ParityConfig

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "pyjanitor_complete"
SKRUB_STUDY = ROOT / "case_studies" / "skrub_agg_joiner"
JOIN_STUDY = ROOT / "case_studies" / "pandas_polars_join"
ASOF_STUDY = ROOT / "case_studies" / "pandas_polars_asof"
STABILITY_STUDY = ROOT / "case_studies" / "stability_probe"
SKRUB_COMMIT = "55dc7f45e140ccb76e768e3e4b4193f4eac3d5aa"
PYJANITOR_COMMIT = "c1b57b993dca4348e9acc41301fe8526dcae57df"
PYJANITOR_FINDINGS = ["null-key-preservation", "narrow-domain-preservation"]

SKRUB_CASES = [
    "aggregate-numeric-control",
    "aggjoiner-numeric-control",
    "aggregate-unique-mode-control",
    "aggregate-arrow-null-control",
    "aggregate-null-key-finding",
    "aggjoiner-tied-mode-finding",
    "aggjoiner-ieee-nan-finding",
]


def _frame_arguments(case) -> list[FrameArgument]:
    assert case.invocation is not None
    return [
        argument
        for argument in [*case.invocation.args, *case.invocation.kwargs.values()]
        if isinstance(argument, FrameArgument)
    ]


def test_pyjanitor_case_study_config_and_evidence_are_consistent() -> None:
    raw = tomllib.loads((STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    report = json.loads((STUDY / "report.json").read_text(encoding="utf-8"))

    case_names = [case.name for case in config.cases]
    report_names = [case["name"] for case in report["cases"]]
    assert len(case_names) == 16
    assert report_names == PYJANITOR_FINDINGS
    assert sum(case["examples_run"] for case in report["cases"]) == 2
    assert all(case["status"] == "failed" for case in report["cases"])
    assert report["schema_version"] == 3
    assert report["parity_version"] == "0.8.1"

    focused_cases = {case.name: case for case in config.cases if case.name in report_names}
    for name in PYJANITOR_FINDINGS:
        for endpoint in (focused_cases[name].reference, focused_cases[name].candidate):
            assert endpoint.record_distributions == ["pyjanitor"]
        provenance = report["cases"][report_names.index(name)]["provenance"]
        assert provenance["verification"] == "captured"
        for endpoint in ("reference", "candidate"):
            distributions = {
                item["name"]: item["version"] for item in provenance[endpoint]["distributions"]
            }
            assert distributions["pyjanitor"] == "0.32.23"
            assert distributions["pandas"] == "3.0.5"
            assert distributions["polars"] == "1.43.2"
            assert distributions["pyarrow"] == "25.0.1"

    for case in config.cases:
        for argument in _frame_arguments(case):
            if argument.fixture is not None:
                assert (STUDY / argument.fixture).is_file()


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
    assert PYJANITOR_COMMIT in content
    assert "parity-check==0.8.1" in content
    assert "pyjanitor's current `dev` head" in content
    assert "pandas==3.0.5" in content
    assert "polars==1.43.2" in content
    assert "pyarrow==25.0.1" in content
    assert "exits `1`" in content


def test_pyjanitor_issue_drafts_include_standalone_reproductions() -> None:
    content = (STUDY / "UPSTREAM_ISSUES.md").read_text(encoding="utf-8")
    assert "loses payload values when completion keys contain nulls" in content
    assert "drops existing rows outside an explicit domain" in content
    assert content.count("import janitor.polars") == 2
    assert content.count(PYJANITOR_COMMIT) >= 2
    assert "import parity" not in content
    assert content.count("Actual:") == 2
    assert content.count("Expected:") == 2
    assert "found no equivalent report" in content


def test_skrub_case_study_config_and_reports_are_consistent() -> None:
    raw = tomllib.loads((SKRUB_STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)

    assert [case.name for case in config.cases] == SKRUB_CASES
    assert all(case.generation.max_examples == 1 for case in config.cases)
    assert all(not case.generation.adversarial_examples for case in config.cases)
    assert all(not case.performance.enabled for case in config.cases)
    for case in config.cases:
        [argument] = _frame_arguments(case)
        assert argument.fixture is not None
        assert (SKRUB_STUDY / argument.fixture).is_file()
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


def test_join_case_study_uses_a_generated_two_frame_contract() -> None:
    raw = tomllib.loads((JOIN_STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    case = config.cases[0]

    assert case.invocation is not None
    assert list(case.invocation.kwargs) == ["left", "right"]
    assert case.invocation.relationships[0].kind == "equal_row_count"
    assert case.generation.max_examples == 500
    assert case.generation.max_findings == 1
    assert not case.generation.adversarial_examples
    assert not case.performance.enabled
    assert all(spec.input_schema is not None for spec in case.invocation.kwargs.values())

    source = (JOIN_STUDY / "join_parity.py").read_text(encoding="utf-8")
    compile(source, str(JOIN_STUDY / "join_parity.py"), "exec")
    assert "def pandas_left_join(" in source
    assert "def polars_left_join(" in source
    study_readme = (JOIN_STUDY / "README.md").read_text(encoding="utf-8")
    assert "join_nulls=True" in study_readme
    assert "nulls_equal=True" in study_readme
    assert 'left.join(right, on="key", how="left")' in source
    assert 'left.join(right, on="key", how="left", maintain_order=' not in source
    assert ".parity-join/" in (JOIN_STUDY / ".gitignore").read_text(encoding="utf-8")


def test_join_case_study_targets_execute_on_supported_core_dependencies() -> None:
    targets = runpy.run_path(str(JOIN_STUDY / "join_parity.py"))
    rows = {
        "key": [1, 2],
        "left_value": [10, 20],
    }
    right_rows = {
        "key": [1, 2],
        "right_value": [100, 200],
    }

    pandas_result = targets["pandas_left_join"](pd.DataFrame(rows), pd.DataFrame(right_rows))
    polars_result = targets["polars_left_join"](pl.DataFrame(rows), pl.DataFrame(right_rows))

    assert pandas_result.to_dict(orient="list") == polars_result.to_dict(as_series=False)


def test_asof_case_study_declares_valid_order_and_row_domains() -> None:
    raw = tomllib.loads((ASOF_STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    asof_case, interval_case = config.cases

    assert asof_case.invocation is not None
    assert list(asof_case.invocation.kwargs) == ["left", "right"]
    for spec in asof_case.invocation.kwargs.values():
        assert spec.input_schema is not None
        constraint = spec.input_schema.constraints[0]
        assert constraint.kind == "sorted_by"
        assert constraint.columns == ["time"]
    assert interval_case.invocation is not None
    interval_argument = interval_case.invocation.args[0]
    assert isinstance(interval_argument, FrameArgument)
    assert interval_argument.input_schema is not None
    comparison = interval_argument.input_schema.constraints[0]
    assert comparison.kind == "row_comparison"
    assert (comparison.left, comparison.operator, comparison.right) == ("start", "le", "end")

    targets = runpy.run_path(str(ASOF_STUDY / "asof_parity.py"))
    left = {"time": [1], "left_value": [10]}
    right = {"time": [0, 2], "right_value": [100, 200]}
    backward = targets["pandas_backward"](pd.DataFrame(left), pd.DataFrame(right))
    forward = targets["polars_forward"](pl.DataFrame(left), pl.DataFrame(right))
    assert backward["right_value"].tolist() == [100]
    assert forward["right_value"].to_list() == [200]
    assert ".parity-asof/" in (ASOF_STUDY / ".gitignore").read_text(encoding="utf-8")


def test_stability_study_has_a_matching_but_changing_pair() -> None:
    raw = tomllib.loads((STABILITY_STUDY / "parity.toml").read_text(encoding="utf-8"))
    config = ParityConfig.model_validate(raw)
    case = config.cases[0]
    assert case.generation.stability_repeats == 2
    [argument] = _frame_arguments(case)
    assert argument.fixture is not None
    assert (STABILITY_STUDY / argument.fixture).is_file()

    targets = runpy.run_path(str(STABILITY_STUDY / "stability_parity.py"))
    frame = pa.table({"value": [1]})
    first_reference = targets["reference"](frame)
    first_candidate = targets["candidate"](frame)
    second_reference = targets["reference"](frame)
    second_candidate = targets["candidate"](frame)
    assert first_reference.equals(first_candidate)
    assert second_reference.equals(second_candidate)
    assert not first_reference.equals(second_reference)
    assert ".parity-stability/" in (STABILITY_STUDY / ".gitignore").read_text(encoding="utf-8")
