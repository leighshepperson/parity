"""Parity command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from parity import __version__
from parity.config import ConfigError, load_config
from parity.doctor import diagnose
from parity.models import Status

app = typer.Typer(
    name="parity",
    help="Verify that a changed computation still means the same thing.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
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
) -> None:
    """Create a documented starter configuration and example transformations."""

    from parity.templates import write_starter

    try:
        created = write_starter(path, force=force)
    except FileExistsError:
        _fail(f"{path} already exists; pass --force to replace it")
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
) -> None:
    """Check local dependencies without exposing secrets or source code."""

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


if __name__ == "__main__":  # pragma: no cover
    app()
