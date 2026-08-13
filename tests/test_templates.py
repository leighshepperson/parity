from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from parity.config import load_config
from parity.templates import (
    render_config_template,
    render_example_module,
    write_config_template,
    write_starter,
)


def test_generated_config_parses_and_validates(tmp_path: Path) -> None:
    rendered = render_config_template(
        reference="package.old:calculate",
        candidate="package.new:calculate",
        case_name="critical-orders",
    )
    raw = tomllib.loads(rendered)
    assert raw["version"] == 1
    assert raw["cases"][0]["schema"]["columns"][0]["name"] == "quantity"
    assert raw["cases"][0]["reference"]["record_distributions"] == []
    assert raw["cases"][0]["candidate"]["pandas_input"] == "arrow"
    assert raw["cases"][0]["schema"]["constraints"] == []
    assert raw["cases"][0]["comparison"]["row_keys"] == []
    assert raw["cases"][0]["generation"]["stability_repeats"] == 2

    path = tmp_path / "nested" / "parity.toml"
    path.parent.mkdir()
    path.write_text(rendered, encoding="utf-8")
    config = load_config(path)
    assert config.cases[0].name == "critical-orders"
    assert config.cases[0].reference.target == "package.old:calculate"
    assert config.cases[0].comparison.row_keys == []
    assert config.cases[0].input_schema is not None
    # Isolated pandas/Polars workers include interpreter startup.  A generated
    # starter must not turn machine speed into a flaky Hypothesis failure.
    assert config.cases[0].generation.deadline_ms is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reference", "invalid target"),
        ("candidate", "module"),
        ("case_name", 'orders"\nmalicious = true'),
    ],
)
def test_template_rejects_values_that_need_toml_escaping(field: str, value: str) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match=r"must be|may contain"):
        render_config_template(**kwargs)


def test_writer_will_not_replace_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text("user work", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_config_template(path)
    assert path.read_text(encoding="utf-8") == "user work"


def test_starter_is_runnable_python_and_atomic_about_existing_files(tmp_path: Path) -> None:
    paths = write_starter(tmp_path / "parity.toml")
    assert paths == [tmp_path / "parity.toml", tmp_path / "parity_example.py"]
    compile(render_example_module(), "parity_example.py", "exec")
    assert load_config(paths[0]).cases[0].candidate.target == "parity_example:candidate"

    paths[1].write_text("user module", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_starter(paths[0], force=False)
    assert paths[1].read_text(encoding="utf-8") == "user module"


def test_starter_force_replaces_both_files(tmp_path: Path) -> None:
    config = tmp_path / "parity.toml"
    example = tmp_path / "parity_example.py"
    config.write_text("old", encoding="utf-8")
    example.write_text("old", encoding="utf-8")
    write_starter(config, force=True)
    assert "version = 1" in config.read_text(encoding="utf-8")
    assert "def candidate" in example.read_text(encoding="utf-8")
