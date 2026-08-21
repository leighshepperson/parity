from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from parity import cli
from parity.execution import ExecutionOutcome, execute_isolated
from parity.models import CallableSpec

runner = CliRunner()
SOURCE_ROOT = Path(__file__).parents[1] / "src"


def _make_target(adapter_path: Path) -> Path:
    target = adapter_path.parent / "bin" / "legacy-target"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    return target


def _serve_spec(adapter_path: Path) -> CallableSpec:
    return CallableSpec(
        command=[
            sys.executable,
            "-m",
            "parity.cli",
            "adapter",
            "serve",
            str(adapter_path),
        ],
        workdir=adapter_path.parent,
        environment={"PYTHONPATH": str(SOURCE_ROOT)},
    )


def test_adapter_init_creates_valid_module_and_exact_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = Path("adapters/reference.py")

    result = runner.invoke(
        cli.app,
        [
            "adapter",
            "init",
            str(adapter_path),
            "--program",
            "bin/reference",
            "--runtime",
            "fortran",
            "--runtime-version",
            "2008",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "created adapters/reference.py",
        "add this endpoint to parity.toml:",
        "[cases.reference]",
        'command = ["parity", "adapter", "serve", "adapters/reference.py"]',
        "next: parity doctor --config parity.toml",
    ]
    source = adapter_path.read_text(encoding="utf-8")
    compile(source, str(adapter_path), "exec")
    assert 'PROGRAM = Path(__file__).resolve().parent / "bin/reference"' in source
    assert 'RuntimeInfo(name="fortran", version="2008")' in source
    assert "TargetRaised," in source
    assert "back to Arrow or JSON" in source


def test_adapter_init_refuses_overwrite_and_force_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = Path("adapter.py")
    adapter_path.write_text("# reviewed project code\n", encoding="utf-8")

    refused = runner.invoke(cli.app, ["adapter", "init", str(adapter_path)])

    assert refused.exit_code == 2
    assert adapter_path.read_text(encoding="utf-8") == "# reviewed project code\n"

    replaced = runner.invoke(
        cli.app,
        [
            "adapter",
            "init",
            str(adapter_path),
            "--runtime",
            "fortran",
            "--runtime-version",
            "2008",
            "--force",
        ],
    )

    assert replaced.exit_code == 0, replaced.output
    assert 'RuntimeInfo(name="fortran", version="2008")' in adapter_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "options",
    [
        ["--program", "../outside"],
        ["--runtime", "invalid runtime"],
        ["--runtime-version", "invalid version"],
    ],
)
def test_adapter_init_rejects_invalid_program_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = Path("adapter.py")

    result = runner.invoke(cli.app, ["adapter", "init", str(adapter_path), *options])

    assert result.exit_code == 2
    assert not adapter_path.exists()


def test_adapter_init_rejects_an_unloadable_destination_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = Path("adapter.txt")

    result = runner.invoke(cli.app, ["adapter", "init", str(adapter_path)])

    assert result.exit_code == 2
    assert ".py suffix" in result.stderr
    assert not adapter_path.exists()


def test_adapter_init_preserves_a_path_with_spaces_as_one_command_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = Path("adapters with spaces/legacy adapter.py")

    result = runner.invoke(cli.app, ["adapter", "init", str(adapter_path)])

    assert result.exit_code == 0, result.output
    command_line = next(
        line for line in result.stdout.splitlines() if line.startswith("command = ")
    )
    argv = json.loads(command_line.removeprefix("command = "))
    assert argv == [
        "parity",
        "adapter",
        "serve",
        "adapters with spaces/legacy adapter.py",
    ]
    assert adapter_path.is_file()


def test_adapter_serve_runs_a_completed_scaffold_through_the_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = tmp_path / "adapter.py"
    initialized = runner.invoke(
        cli.app,
        [
            "adapter",
            "init",
            str(adapter_path),
            "--runtime",
            "fortran",
            "--runtime-version",
            "2008",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    _make_target(adapter_path)
    source = adapter_path.read_text(encoding="utf-8")
    placeholder = (
        "raise AdapterError(\n"
        '        "adapter_not_implemented",\n'
        '        "implement canonical input to target invocation to canonical output",\n'
        "    )"
    )
    assert placeholder in source
    adapter_path.write_text(source.replace(placeholder, "return frame"), encoding="utf-8")
    table = pa.table({"value": [1.5, -2.0]})

    observation = execute_isolated(_serve_spec(adapter_path), table, timeout_seconds=5)

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.table == table
    assert observation.runtime is not None
    assert observation.runtime.runtime_name == "fortran"
    assert observation.runtime.runtime_version == "2008"


def test_generated_placeholder_is_an_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = tmp_path / "adapter.py"
    initialized = runner.invoke(cli.app, ["adapter", "init", str(adapter_path)])
    assert initialized.exit_code == 0, initialized.output
    _make_target(adapter_path)

    observation = execute_isolated(
        _serve_spec(adapter_path),
        pa.table({"value": [1]}),
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.ERROR
    assert observation.exception is not None
    assert observation.exception.type == "AdapterError"
    assert observation.exception.details == {"error_codes": ["adapter_not_implemented"]}


def test_generated_scaffold_can_classify_an_explicit_domain_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter_path = tmp_path / "adapter.py"
    initialized = runner.invoke(cli.app, ["adapter", "init", str(adapter_path)])
    assert initialized.exit_code == 0, initialized.output
    _make_target(adapter_path)
    source = adapter_path.read_text(encoding="utf-8")
    placeholder = (
        "raise AdapterError(\n"
        '        "adapter_not_implemented",\n'
        '        "implement canonical input to target invocation to canonical output",\n'
        "    )"
    )
    adapter_path.write_text(
        source.replace(
            placeholder,
            'raise TargetRaised("rejected", module="legacy.domain", exception_type="InvalidInput")',
        ),
        encoding="utf-8",
    )

    observation = execute_isolated(
        _serve_spec(adapter_path),
        pa.table({"value": [1]}),
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.RAISED
    assert observation.exception is not None
    assert observation.exception.module == "legacy.domain"
    assert observation.exception.type == "InvalidInput"
