from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parity import templates
from parity.config import load_config
from parity.templates import (
    render_config_template,
    render_example_module,
    render_project_config,
    write_config_template,
    write_project_config,
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


def test_project_template_is_minimal_fixture_backed_and_allows_same_target() -> None:
    rendered = render_project_config(
        reference="project.transform:run",
        candidate="project.transform:run",
        fixture="fixtures/input.json",
        case_name="polars-versions",
        reference_adapter="polars",
        candidate_adapter="polars",
        reference_python=".venv-old/bin/python",
        candidate_python=".venv-new/bin/python",
        record_distributions=["Polars"],
        row_keys=["account_id", "period"],
    )
    raw = tomllib.loads(rendered)
    case = raw["cases"][0]
    assert case["fixture"] == "fixtures/input.json"
    assert case["reference"]["target"] == case["candidate"]["target"]
    assert case["reference"]["record_distributions"] == ["polars"]
    assert case["comparison"] == {
        "row_order": "keyed",
        "row_keys": ["account_id", "period"],
    }
    assert "schema" not in case
    assert "generation" not in case


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reference": "not a target"}, "reference must be"),
        ({"candidate_adapter": "spark"}, "candidate_adapter must be"),
        ({"case_name": "bad name"}, "case_name may contain"),
        ({"row_keys": ["id", "id"]}, "row keys must be unique"),
        ({"record_distributions": ["bad/name"]}, "distribution names"),
    ],
)
def test_project_template_rejects_invalid_options(kwargs: dict[str, object], message: str) -> None:
    options: dict[str, object] = {
        "reference": "project.old:run",
        "candidate": "project.new:run",
        "fixture": "fixture.json",
    }
    options.update(kwargs)
    with pytest.raises((TypeError, ValueError), match=message):
        render_project_config(**options)  # type: ignore[arg-type]


def test_project_writer_validates_fixture_and_preserves_relative_paths(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "input.parquet"
    fixture.parent.mkdir()
    pq.write_table(pa.table({"id": [1, 2]}), fixture)
    config_path = tmp_path / "config" / "parity.toml"

    written = write_project_config(
        config_path,
        reference="project.old:run",
        candidate="project.new:run",
        fixture=fixture,
        reference_python=sys.executable,
        candidate_python=sys.executable,
    )
    assert written == config_path
    config = load_config(written)
    assert config.cases[0].fixture == fixture.resolve()
    assert config.cases[0].reference.python == Path(sys.executable)
    assert not (config_path.parent / "parity_example.py").exists()


def test_project_writer_preserves_distinct_virtualenv_python_symlinks(tmp_path: Path) -> None:
    fixture = tmp_path / "input.parquet"
    pq.write_table(pa.table({"id": [1]}), fixture)
    interpreters: list[Path] = []
    for name in ("old", "new"):
        interpreter = tmp_path / name / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        interpreters.append(interpreter)

    config_path = tmp_path / "config" / "parity.toml"
    write_project_config(
        config_path,
        reference="project.transform:run",
        candidate="project.transform:run",
        fixture=fixture,
        reference_python=interpreters[0],
        candidate_python=interpreters[1],
    )

    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))["cases"][0]
    assert raw["reference"]["python"] != raw["candidate"]["python"]
    case = load_config(config_path).cases[0]
    assert case.reference.python == interpreters[0]
    assert case.candidate.python == interpreters[1]
    assert case.reference.python != case.candidate.python


def test_project_writer_validates_before_replacing_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "parity.toml"
    destination.write_text("user work", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_project_config(
            destination,
            reference="project.old:run",
            candidate="project.new:run",
            fixture=tmp_path / "missing.json",
        )
    assert destination.read_text(encoding="utf-8") == "user work"


def test_project_writer_rejects_invalid_fixture_without_partial_file(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("not supported", encoding="utf-8")
    destination = tmp_path / "nested" / "parity.toml"
    with pytest.raises(ValueError, match="unsupported fixture extension"):
        write_project_config(
            destination,
            reference="project.old:run",
            candidate="project.new:run",
            fixture=fixture,
        )
    assert not destination.exists()


def test_atomic_writer_leaves_existing_file_intact_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "parity.toml"
    destination.write_text("user work", encoding="utf-8")

    def reject_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(templates.os, "replace", reject_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_config_template(destination, force=True)
    assert destination.read_text(encoding="utf-8") == "user work"
    assert list(tmp_path.glob(".parity.toml.*.tmp")) == []
