from __future__ import annotations

import importlib.util
import stat
from pathlib import Path
from types import ModuleType

import pyarrow as pa

from parity.config import load_config

STUDY = Path(__file__).parents[1] / "case_studies" / "fortran_python"


def _candidate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fortran_python_candidate", STUDY / "candidate.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fortran_python_case_study_has_replayable_local_boundaries() -> None:
    config = load_config(STUDY / "parity.toml")

    assert [case.name for case in config.cases] == [
        "correct-port",
        "naive-port-cancellation",
    ]
    for case in config.cases:
        assert case.reference.command == ["./fortran_adapter.py"]
        assert case.reference.workdir == STUDY
        assert case.candidate.workdir == STUDY
        assert not case.performance.enabled
        assert case.comparison.rtol == 0
        assert case.comparison.atol == 0
    assert config.cases[0].candidate.target == "candidate:correct_port"
    assert config.cases[1].candidate.target == "candidate:naive_port"
    assert (STUDY / "fortran_adapter.py").stat().st_mode & stat.S_IXUSR


def test_fortran_python_case_study_models_a_real_cancellation_defect() -> None:
    candidate = _candidate()
    ordinary = pa.table({"value": pa.array([0.125, -0.5, 2.0, 3.375], pa.float64())})
    cancellation = pa.table(
        {
            "value": pa.array(
                [2**53, 1.0, -(2**53)],
                pa.float64(),
            )
        }
    )

    assert candidate.correct_port(ordinary) == candidate.naive_port(ordinary) == 5.0
    assert candidate.correct_port(cancellation) == 1.0
    assert candidate.naive_port(cancellation) == 0.0
