from __future__ import annotations

from pathlib import Path

from parity.config import load_config

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "pydantic_version"


def test_pydantic_version_campaign_is_a_real_dependency_isolation_contract() -> None:
    config = load_config(STUDY / "parity.toml")

    assert [case.name for case in config.cases] == [
        "stable-model-validation",
        "pydantic-v1-to-v2-semantics",
    ]
    control, migration = config.cases
    assert "control" in control.tags
    assert migration.generation.max_findings == 4
    assert migration.reference.target == migration.candidate.target
    assert migration.reference.required_distributions == {"pydantic": "<2"}
    assert migration.candidate.required_distributions == {"pydantic": ">=2"}
    assert migration.reference.python != migration.candidate.python

    for side in ("reference", "candidate"):
        requirements = (STUDY / "environments" / side / "requirements.txt").read_text(
            encoding="utf-8"
        )
        assert "pydantic==" in requirements
        assert "pyarrow==" in requirements
        assert "pandas" not in requirements
        assert "parity" not in requirements.casefold()
