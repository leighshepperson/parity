from __future__ import annotations

import sys
from pathlib import Path

import pytest

from parity.config import ConfigError, load_config

VALID = """
version = 1
artifact_dir = "artifacts"

[[cases]]
name = "orders"
fixture = "fixtures/orders.csv"

[cases.reference]
target = "transforms:legacy"
pandas_input = "native"
record_distributions = ["Scikit_Learn", "skrub"]

[cases.candidate]
target = "transforms:rewritten"
adapter = "polars"
"""


def test_load_config_resolves_paths_from_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "parity.toml"
    config_path.parent.mkdir()
    config_path.write_text(VALID, encoding="utf-8")

    config = load_config(config_path)

    assert config.artifact_dir == (config_path.parent / "artifacts").resolve()
    assert config.cases[0].fixture == (config_path.parent / "fixtures/orders.csv").resolve()
    assert config.cases[0].reference.workdir == config_path.parent.resolve()
    assert config.cases[0].reference.pandas_input == "native"
    assert config.cases[0].reference.record_distributions == ["scikit-learn", "skrub"]
    assert config.cases[0].candidate.pandas_input == "arrow"


def test_load_config_reports_toml_error(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text("[[", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_load_config_reports_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(VALID + "\nunknown = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid Parity configuration"):
        load_config(path)


def test_load_config_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration not found"):
        load_config(tmp_path / "missing.toml")


def test_load_config_rejects_partial_input_bundle_fixtures(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        """
version = 1

[[cases]]
name = "join"

[cases.reference]
target = "transforms:legacy"

[cases.candidate]
target = "transforms:rewritten"

[cases.input_bundle.inputs.left]
fixture = "left.csv"

[cases.input_bundle.inputs.right.schema]
min_rows = 0
max_rows = 2

[[cases.input_bundle.inputs.right.schema.columns]]
name = "id"
dtype = "integer"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="provided for every input or none"):
        load_config(path)


def test_load_config_preserves_positional_bundle_order_and_resolves_fixtures(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        """
version = 1

[[cases]]
name = "join"

[cases.reference]
target = "transforms:legacy"

[cases.candidate]
target = "transforms:rewritten"

[cases.input_bundle]
binding = "positional"

[cases.input_bundle.inputs.zebra]
fixture = "fixtures/zebra.csv"

[cases.input_bundle.inputs.alpha]
fixture = "fixtures/alpha.csv"
""",
        encoding="utf-8",
    )

    bundle = load_config(path).cases[0].input_bundle

    assert bundle is not None
    assert list(bundle.inputs) == ["zebra", "alpha"]
    assert bundle.inputs["zebra"].fixture == (tmp_path / "fixtures/zebra.csv").resolve()
    assert bundle.inputs["alpha"].fixture == (tmp_path / "fixtures/alpha.csv").resolve()


def test_load_config_preserves_distinct_virtualenv_python_symlink_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "parity.toml"
    for name in ("old", "new"):
        executable = tmp_path / name / "bin" / "python"
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
    config_path.write_text(
        """
version = 1

[[cases]]
name = "versions"
fixture = "fixture.json"

[cases.reference]
target = "transform:run"
python = "old/bin/python"

[cases.candidate]
target = "transform:run"
python = "new/bin/python"
""",
        encoding="utf-8",
    )

    case = load_config(config_path).cases[0]

    assert case.reference.python == tmp_path / "old" / "bin" / "python"
    assert case.candidate.python == tmp_path / "new" / "bin" / "python"
    assert case.reference.python != case.candidate.python
    assert case.reference.python.resolve() == case.candidate.python.resolve()
