"""Parity command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from parity import __version__
from parity.config import ConfigError, load_config
from parity.doctor import ConfigDoctorReport, WorkerRuntimeReport, diagnose, diagnose_config
from parity.models import Status

if TYPE_CHECKING:
    from parity.migration import MigrationResult

app = typer.Typer(
    name="parity",
    help="Verify that a changed computation still means the same thing.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
migration_app = typer.Typer(
    help="Check that every declared migration unit is covered and passing.",
    no_args_is_help=True,
)
app.add_typer(migration_app, name="migration")
console = Console()
error_console = Console(stderr=True)


def _fail(message: str, code: int = 2) -> None:
    error_console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code)


@app.command("version")
def version_command() -> None:
    """Print the installed Parity version."""

    console.print(__version__)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Configuration path")] = Path("parity.toml"),
    force: Annotated[bool, typer.Option("--force", help="Replace an existing file")] = False,
    reference: Annotated[
        str | None,
        typer.Option("--reference", help="Reference import target for project setup"),
    ] = None,
    candidate: Annotated[
        str | None,
        typer.Option("--candidate", help="Candidate import target for project setup"),
    ] = None,
    fixture: Annotated[
        Path | None,
        typer.Option("--fixture", help="Fixture for the generated project case"),
    ] = None,
    case_name: Annotated[
        str | None,
        typer.Option("--case-name", help="Name for the generated project case"),
    ] = None,
    reference_adapter: Annotated[
        str,
        typer.Option("--reference-adapter", help="auto, pandas, polars or arrow"),
    ] = "auto",
    candidate_adapter: Annotated[
        str,
        typer.Option("--candidate-adapter", help="auto, pandas, polars or arrow"),
    ] = "auto",
    reference_python: Annotated[
        Path | None,
        typer.Option("--reference-python", help="Python executable for the reference worker"),
    ] = None,
    candidate_python: Annotated[
        Path | None,
        typer.Option("--candidate-python", help="Python executable for the candidate worker"),
    ] = None,
    record_distribution: Annotated[
        list[str] | None,
        typer.Option(
            "--record-distribution",
            help="Distribution version to inspect in both workers; repeatable",
        ),
    ] = None,
    row_key: Annotated[
        list[str] | None,
        typer.Option("--row-key", help="Unique output key column; repeatable"),
    ] = None,
) -> None:
    """Create a starter or one fixture-backed project configuration."""

    from parity.templates import write_project_config, write_starter

    required = (reference, candidate, fixture)
    supplied = sum(value is not None for value in required)
    project_options = any(
        (
            case_name is not None,
            reference_adapter != "auto",
            candidate_adapter != "auto",
            reference_python is not None,
            candidate_python is not None,
            bool(record_distribution),
            bool(row_key),
        )
    )
    if supplied not in {0, 3}:
        _fail("--reference, --candidate and --fixture must be provided together")
    if supplied == 0 and project_options:
        _fail("project setup options require --reference, --candidate and --fixture")

    try:
        if supplied == 0:
            created = write_starter(path, force=force)
        else:
            assert reference is not None
            assert candidate is not None
            assert fixture is not None
            created = [
                write_project_config(
                    path,
                    reference=reference,
                    candidate=candidate,
                    fixture=fixture,
                    case_name=case_name or "migration",
                    reference_adapter=reference_adapter,
                    candidate_adapter=candidate_adapter,
                    reference_python=reference_python,
                    candidate_python=candidate_python,
                    record_distributions=record_distribution or (),
                    row_keys=row_key or (),
                    force=force,
                )
            ]
    except FileExistsError:
        _fail(f"{path} already exists; pass --force to replace it")
    except Exception as exc:
        _fail(str(exc))
    for item in created:
        console.print(f"[green]created[/green] {item}")


@app.command("inspect")
def inspect_fixture(
    fixture: Annotated[Path, typer.Argument(exists=True, readable=True)],
    max_rows: Annotated[int, typer.Option(min=1, max=10_000)] = 30,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Infer a portable input schema from a local fixture."""

    from parity.adapters import load_fixture
    from parity.schema import infer_schema

    try:
        schema = infer_schema(load_fixture(fixture), max_rows=max_rows)
    except Exception as exc:
        _fail(str(exc))
    rendered = schema.model_dump_json(indent=2)
    if output is not None:
        output.write_text(rendered + "\n", encoding="utf-8")
        console.print(f"[green]wrote[/green] {output}")
    else:
        console.print_json(rendered)


@app.command()
def check(
    config_path: Annotated[Path, typer.Option("--config", "-c", help="Path to parity.toml")] = Path(
        "parity.toml"
    ),
    case: Annotated[
        list[str] | None, typer.Option("--case", help="Run only a named case; repeatable")
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", help="Run cases carrying this tag; repeatable")
    ] = None,
    max_examples: Annotated[
        int | None, typer.Option("--max-examples", min=1, help="Override generated examples")
    ] = None,
    max_findings: Annotated[
        int | None,
        typer.Option("--max-findings", min=1, max=20, help="Override distinct findings"),
    ] = None,
    stability_repeats: Annotated[
        int | None,
        typer.Option(
            "--stability-repeats",
            min=1,
            max=10,
            help="Override same-input observations per implementation",
        ),
    ] = None,
    performance: Annotated[
        bool, typer.Option("--performance/--no-performance", help="Run performance checks")
    ] = True,
    json_output: Annotated[Path | None, typer.Option("--json")] = None,
    junit_output: Annotated[Path | None, typer.Option("--junit")] = None,
    markdown_output: Annotated[Path | None, typer.Option("--markdown")] = None,
) -> None:
    """Run semantic verification campaigns."""

    from parity.engine import run_suite
    from parity.reporting import render_markdown, render_terminal, write_json, write_junit

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))

    selected = set(case or [])
    requested_tags = set(tag or [])
    if requested_tags:
        known_tags = {label for item in config.cases for label in item.tags}
        unknown_tags = requested_tags - known_tags
        if unknown_tags:
            _fail(f"unknown tag(s): {', '.join(sorted(unknown_tags))}")
        selected.update(
            item.name for item in config.cases if requested_tags.intersection(item.tags)
        )
    known = {item.name for item in config.cases}
    unknown = selected - known
    if unknown:
        _fail(f"unknown case(s): {', '.join(sorted(unknown))}")
    for item in config.cases:
        if max_examples is not None:
            item.generation.max_examples = max_examples
        if max_findings is not None:
            item.generation.max_findings = max_findings
        if stability_repeats is not None:
            item.generation.stability_repeats = stability_repeats
        if not performance:
            item.performance.enabled = False

    result = run_suite(config, selected_cases=selected or None)
    render_terminal(result, console=console)
    if json_output is not None:
        write_json(result, json_output)
    if junit_output is not None:
        write_junit(result, junit_output)
    markdown = render_markdown(result)
    if markdown_output is not None:
        markdown_output.write_text(markdown, encoding="utf-8")
    # GitHub supplies this path. Avoid reading or writing any other environment variables.
    import os

    if github_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        summary_file = Path(github_summary)
        with summary_file.open("a", encoding="utf-8") as stream:
            stream.write(markdown)
            if not markdown.endswith("\n"):
                stream.write("\n")
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


def _render_migration_result(result: MigrationResult) -> None:
    """Render the bounded migration ledger without result values or local paths."""

    from parity.execution import redact_text
    from parity.migration import migration_summary

    colors = {
        "passed": "green",
        "failed": "red",
        "error": "red",
        "excluded": "yellow",
        "uncovered": "yellow",
    }
    table = Table("Migration unit", "Status", "Mapped cases")
    for unit in result.units:
        mapped = (
            ", ".join(f"{redact_text(case.name)} ({case.status.value})" for case in unit.cases)
            or "—"
        )
        status = unit.status.value
        table.add_row(
            redact_text(unit.id), f"[{colors[status]}]{status}[/{colors[status]}]", mapped
        )
    console.print(table)

    summary = migration_summary(result)
    console.print(
        " · ".join(
            [
                f"{summary['passed']} passed",
                f"{summary['failed']} failed",
                f"{summary['error']} error",
                f"{summary['excluded']} excluded",
                f"{summary['uncovered']} uncovered",
            ]
        )
    )
    if result.status is Status.PASSED:
        console.print("[green]all declared in-scope migration units passed[/green]")
    elif result.status is Status.ERROR:
        console.print("[red]migration coverage could not be established[/red]")
    else:
        console.print("[red]migration incomplete[/red]")


@migration_app.command("check")
def migration_check(
    manifest_path: Annotated[
        Path,
        typer.Option("--manifest", "-m", help="Path to migration.toml"),
    ] = Path("migration.toml"),
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to parity.toml"),
    ] = Path("parity.toml"),
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write the data-safe migration report"),
    ] = None,
) -> None:
    """Run the complete declared migration coverage gate."""

    from parity.migration import (
        MigrationConfigError,
        check_migration,
        write_migration_json,
    )

    try:
        result = check_migration(manifest_path, config_path)
    except (ConfigError, MigrationConfigError) as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"migration coverage could not run ({type(exc).__name__})")

    if json_output is not None:
        try:
            write_migration_json(result, json_output)
        except OSError as exc:
            _fail(f"migration report could not be written ({type(exc).__name__})")
    _render_migration_result(result)
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


@app.command()
def replay(
    artifact: Annotated[Path, typer.Argument(exists=True, readable=True)],
) -> None:
    """Re-run a preserved counterexample exactly."""

    from parity.engine import replay_artifact
    from parity.reporting import render_terminal

    try:
        result = replay_artifact(artifact)
    except Exception as exc:
        _fail(str(exc))
    render_terminal(result, console=console)
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable output")] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Inspect workers from this parity.toml"),
    ] = None,
    case: Annotated[
        str | None,
        typer.Option("--case", help="Inspect only one configured case"),
    ] = None,
) -> None:
    """Check local dependencies or configured worker environments."""

    if case is not None and config_path is None:
        _fail("--case requires --config")
    if config_path is not None:
        try:
            config_report = diagnose_config(load_config(config_path), case_name=case)
        except ConfigError:
            _fail("configuration could not be loaded or validated")
        except ValueError as exc:
            _fail(str(exc))
        if as_json:
            console.print_json(json.dumps(config_report.to_dict()))
        else:
            _render_config_doctor(config_report)
        if not config_report.healthy:
            raise typer.Exit(2)
        return

    report = diagnose()
    if as_json:
        console.print_json(json.dumps(report.to_dict()))
    else:
        console.print(f"Python {report.python} · {report.platform}")
        table = Table("Dependency", "Version", "Status")
        for dependency in report.dependencies:
            table.add_row(
                dependency.name,
                dependency.version or "—",
                "[green]ready[/green]" if dependency.installed else "[red]missing[/red]",
            )
        console.print(table)
    if not report.healthy:
        raise typer.Exit(2)


def _runtime_label(worker: WorkerRuntimeReport, field: str) -> str:
    if worker.status != "ready":
        return worker.status.replace("_", " ")
    if field == "python":
        return f"{worker.python_implementation} {worker.python_version}"
    if field == "parity":
        return worker.parity_version or "unavailable"
    distributions = {item.name: item for item in worker.distributions}
    item = distributions.get(field)
    if item is None:
        return "not requested"
    return item.version if item.status == "installed" and item.version is not None else item.status


def _render_config_doctor(report: ConfigDoctorReport) -> None:
    table = Table("Case", "Runtime", "Reference", "Candidate")
    for case_report in report.cases:
        fields = ["python", "parity"]
        fields.extend(
            sorted(
                {
                    item.name
                    for worker in (case_report.reference, case_report.candidate)
                    for item in worker.distributions
                }
            )
        )
        for field in fields:
            label = "Python" if field == "python" else "Parity" if field == "parity" else field
            table.add_row(
                case_report.name,
                label,
                _runtime_label(case_report.reference, field),
                _runtime_label(case_report.candidate, field),
            )
    console.print(table)
    if report.healthy:
        console.print("[green]configured workers ready[/green]")
    else:
        console.print("[red]configured workers not ready[/red]")


if __name__ == "__main__":  # pragma: no cover
    app()
