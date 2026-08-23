from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from parity import Invocation
from parity.config import load_config

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "javascript_python_rules"


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _capture(target: Callable[..., object], invocation: Invocation) -> tuple[str, object]:
    try:
        return "returned", target(*invocation.args, **invocation.kwargs)
    except Exception as error:
        return "raised", (type(error).__module__, type(error).__qualname__, str(error))


def test_rules_study_is_a_json_only_complete_call_contract() -> None:
    raw = tomllib.loads((STUDY / "parity.toml").read_text(encoding="utf-8"))
    cases = raw["cases"]

    assert [case["name"] for case in cases] == [
        "correct-port",
        "naive-port",
        "regression-eager-score",
        "regression-first-match",
        "regression-inclusive-threshold",
    ]
    assert all("invocation" not in case for case in cases)
    assert all(case["generation"]["generator"].startswith("generator:") for case in cases)
    assert all(
        case["reference"]["command"] == ["parity", "adapter", "serve", "reference_adapter.py"]
        for case in cases
    )
    assert all(case["candidate"]["target"].startswith("candidate:") for case in cases)

    config = load_config(STUDY / "parity.toml")
    assert config.artifact_dir == STUDY / ".parity-rules"
    assert all(case.invocation is None for case in config.cases)
    assert all(case.generation.generator is not None for case in config.cases)

    authored_contract = "\n".join(
        (STUDY / name).read_text(encoding="utf-8")
        for name in ("parity.toml", "candidate.py", "generator.py", "legacy_rules.js")
    ).lower()
    assert "pandas" not in authored_contract
    assert "polars" not in authored_contract
    assert "pyarrow" not in authored_contract


def test_retained_rules_regressions_cover_three_independent_defects() -> None:
    candidate = _load_module("rules_candidate", STUDY / "candidate.py")
    generator = _load_module("rules_generator", STUDY / "generator.py")
    regressions = generator.REGRESSIONS

    eager = (
        _capture(candidate.correct_port, regressions[0]),
        _capture(candidate.naive_port, regressions[0]),
    )
    first_match = (
        _capture(candidate.correct_port, regressions[1]),
        _capture(candidate.naive_port, regressions[1]),
    )
    threshold = (
        _capture(candidate.correct_port, regressions[2]),
        _capture(candidate.naive_port, regressions[2]),
    )

    assert (eager[0][0], eager[1][0]) == ("returned", "raised")
    assert eager[1][1] == ("legacy.rules", "RuleEvaluationError", "unknown variable 'missing'")

    assert first_match[0][0] == first_match[1][0] == "returned"
    assert first_match[0][1]["total"] == 3
    assert first_match[1][1]["total"] == 1
    assert len(first_match[0][1]["trace"]) == 2
    assert len(first_match[1][1]["trace"]) == 1

    assert threshold[0][0] == threshold[1][0] == "returned"
    assert threshold[0][1]["decision"] == "allow"
    assert threshold[1][1]["decision"] == "deny"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_reference_matches_python_port_for_returns_and_domain_errors() -> None:
    candidate = _load_module("rules_candidate_node", STUDY / "candidate.py")
    generator = _load_module("rules_generator_node", STUDY / "generator.py")
    invocations = list(generator.REGRESSIONS)
    invocations.append(
        Invocation(
            args=(
                {
                    "rules": [
                        {
                            "name": "invalid",
                            "when": {"op": "const", "value": True},
                            "score": {"op": "var", "name": "missing"},
                            "labels": ["invalid"],
                        }
                    ]
                },
                {"x": 0, "y": 0, "z": 0, "enabled": False, "vip": False},
            ),
            kwargs={"threshold": 1},
        )
    )

    node = shutil.which("node")
    assert node is not None
    for invocation in invocations:
        completed = subprocess.run(
            [node, str(STUDY / "legacy_rules.js")],
            input=json.dumps(
                {
                    "program": invocation.args[0],
                    "context": invocation.args[1],
                    "threshold": invocation.kwargs["threshold"],
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        response = json.loads(completed.stdout)
        expected = _capture(candidate.correct_port, invocation)
        if expected[0] == "returned":
            assert response == {"outcome": "returned", "value": expected[1]}
        else:
            assert response == {"outcome": "raised", "message": expected[1][2]}


def test_rules_proof_is_visible_in_readme_and_ci() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "unit of work is an explicit `callable(*args, **kwargs)` contract" in readme
    assert "case_studies/javascript_python_rules/README.md" in readme
    assert "javascript-python-rules" in workflow
    assert "python verify.py --profile quick" in workflow
