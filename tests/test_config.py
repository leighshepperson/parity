from __future__ import annotations

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
