from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_pandas_and_polars_are_named_extras_not_core_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = set(project["dependencies"])
    extras = project["optional-dependencies"]

    assert not any(requirement.startswith(("pandas", "polars")) for requirement in core)
    assert extras["pandas"] == ["pandas>=2.1"]
    assert extras["polars"] == ["polars>=1.0"]
    for group in ("test", "dev"):
        assert "pandas>=2.1" in extras[group]
        assert "polars>=1.0" in extras[group]


def test_managed_environment_tools_are_core_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = set(project["dependencies"])
    extras = project["optional-dependencies"]

    assert {"tox>=4.44", "tox-uv>=1.29", "uv>=0.9.1"} <= core
    assert "workspace" not in extras


def test_bare_core_import_and_doctor_do_not_import_optional_engines() -> None:
    script = f"""
import importlib.abc
import sys

sys.path.insert(0, {str(ROOT / "src")!r})

class BlockOptionalEngines(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] in {{'pandas', 'polars'}}:
            error = ModuleNotFoundError(f'blocked optional dependency: {{fullname}}')
            error.name = fullname
            raise error
        return None

sys.meta_path.insert(0, BlockOptionalEngines())

import parity
from parity.adapters import AdapterError, available_adapters, get_adapter
from parity.doctor import REQUIRED_DEPENDENCIES, diagnose

assert parity.__version__
assert available_adapters() == ('arrow',)
assert 'pandas' not in sys.modules
assert 'polars' not in sys.modules
assert 'pandas' not in REQUIRED_DEPENDENCIES
assert 'polars' not in REQUIRED_DEPENDENCIES
assert diagnose().healthy

for name in ('pandas', 'polars'):
    try:
        get_adapter(name)
    except AdapterError as error:
        assert f'install parity-check[{{name}}]' in str(error)
    else:
        raise AssertionError(f'expected missing {{name}} adapter')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_lazy_optional_detection_supports_user_defined_subclasses() -> None:
    script = f"""
import sys

sys.path.insert(0, {str(ROOT / "src")!r})

import pandas as pd
import polars as pl

from parity.adapters import detect_adapter
from parity.canonical import CanonicalSeries, canonicalize

class PandasFrame(pd.DataFrame):
    pass

class PandasSeries(pd.Series):
    pass

class PolarsFrame(pl.DataFrame):
    pass

class PolarsSeries(pl.Series):
    pass

assert isinstance(canonicalize(PandasSeries([1], name='value')), CanonicalSeries)
assert isinstance(canonicalize(PolarsSeries('value', [1])), CanonicalSeries)
assert detect_adapter(PandasFrame({{'value': [1]}})).name == 'pandas'
assert detect_adapter(PolarsFrame({{'value': [1]}})).name == 'polars'
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
