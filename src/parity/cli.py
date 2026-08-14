"""Parity command-line interface."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

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
evidence_app = typer.Typer(
    help="Verify retained counterexamples referenced by a Parity report.",
    no_args_is_help=True,
)
app.add_typer(migration_app, name="migration")
app.add_typer(evidence_app, name="evidence")
console = Console()
error_console = Console(stderr=True)


def _fail(message: str, code: int = 2) -> None:
    rendered = Text()
    rendered.append("error:", style="bold red")
    rendered.append(f" {message}")
    error_console.print(rendered)
    raise typer.Exit(code)


def _print_path_status(label: str, path: Path, *, style: str = "green") -> None:
    """Render a user-selected path without interpreting Rich markup."""

    rendered = Text()
    rendered.append(label, style=style)
    rendered.append(f" {path}")
    console.print(rendered)


def _atomic_write_output(path: Path, content: str) -> None:
    """Write one CLI output atomically, including missing parent directories."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
        _print_path_status("created", item)


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
        try:
            _atomic_write_output(output, rendered + "\n")
        except OSError as exc:
            _fail(f"schema output could not be written ({type(exc).__name__})")
        _print_path_status("wrote", output)
    else:
        console.print_json(rendered)


@app.command()
def check(
    config_path: Annotated[Path, typer.Option("--config", "-c", help="Path to parity.toml")] = Path(
        "parity.toml"
    ),
    case: Annotated[
        list[str] | None,
        typer.Option(
            "--case",
            help="Run a named case; repeatable and combined with any --tag matches",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Run cases carrying this tag; repeatable and combined with --case",
        ),
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
    from parity.reporting import render_markdown, render_terminal, write_report

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
    written: list[Path] = []
    try:
        if json_output is not None:
            written.append(write_report(result, "json", json_output))
        if junit_output is not None:
            written.append(write_report(result, "junit", junit_output))
        if markdown_output is not None:
            written.append(write_report(result, "markdown", markdown_output))
    except (OSError, ValueError) as exc:
        _fail(f"verification report could not be written ({type(exc).__name__})")
    markdown = render_markdown(result)
    # GitHub supplies this path. Avoid reading or writing any other environment variables.
    if github_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            summary_file = Path(github_summary)
            summary_file.parent.mkdir(parents=True, exist_ok=True)
            with summary_file.open("a", encoding="utf-8") as stream:
                stream.write(markdown)
                if not markdown.endswith("\n"):
                    stream.write("\n")
        except OSError as exc:
            _fail(f"GitHub summary could not be written ({type(exc).__name__})")
    render_terminal(result, console=console)
    for path in written:
        _print_path_status("wrote", Path(path.name))
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
    if result.suite.status is not Status.PASSED:
        from parity.reporting import render_terminal

        console.print("[bold]case evidence[/bold]")
        render_terminal(result.suite, console=console)


@migration_app.command("init")
def migration_init(
    reference: Annotated[
        str,
        typer.Option(
            "--reference",
            help="Exact released requirement, for example package==1.2.3",
        ),
    ],
    workspace_path: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Workspace file to create"),
    ] = Path("migrations/parity.workspace.toml"),
    candidate: Annotated[
        Path,
        typer.Option("--candidate", help="Existing local candidate checkout"),
    ] = Path("."),
    python_version: Annotated[
        str | None,
        typer.Option("--python", help="Worker Python major.minor; defaults to this Python"),
    ] = None,
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Parity configuration path"),
    ] = Path("migrations/parity.toml"),
    manifest_path: Annotated[
        Path,
        typer.Option("--manifest", "-m", help="Migration ledger path"),
    ] = Path("migrations/migration.toml"),
    report_dir: Annotated[
        Path | None,
        typer.Option("--report-dir", help="Per-lane JSON report directory"),
    ] = None,
    lane: Annotated[
        list[str] | None,
        typer.Option(
            "--lane",
            help="Dependency lane as NAME or NAME=REQUIREMENTS; repeatable",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing workspace file"),
    ] = False,
) -> None:
    """Declare one active released-reference to local-candidate migration."""

    from parity.migration import (
        MigrationConfigError,
        MigrationManifest,
        MigrationUnit,
        load_migration_manifest,
        write_migration_manifest,
    )
    from parity.migration_workspace import (
        WorkspaceError,
        parse_lane_options,
        rebase_workspace_path,
        write_workspace,
    )

    invocation = Path.cwd()
    absolute_workspace = (
        workspace_path if workspace_path.is_absolute() else invocation / workspace_path
    )
    absolute_config = config_path if config_path.is_absolute() else invocation / config_path
    absolute_manifest = manifest_path if manifest_path.is_absolute() else invocation / manifest_path
    if os.path.lexists(absolute_workspace) and not force:
        _fail(f"{workspace_path} already exists; pass --force to replace it")

    generated_manifest = False
    try:
        configured = load_config(absolute_config)
        if os.path.lexists(absolute_manifest):
            load_migration_manifest(absolute_manifest)
        else:
            write_migration_manifest(
                MigrationManifest(
                    units=[
                        MigrationUnit(
                            id="core-regression",
                            cases=[case.name for case in configured.cases],
                        )
                    ]
                ),
                absolute_manifest,
            )
            generated_manifest = True
        effective_report_dir = (
            Path(".parity/workspace/reports")
            if report_dir is None
            else rebase_workspace_path(
                report_dir,
                workspace_path=workspace_path,
                invocation_cwd=invocation,
            )
        )
        if report_dir is not None and ".." in effective_report_dir.parts:
            raise WorkspaceError("--report-dir must stay inside the workspace directory")
        created = write_workspace(
            workspace_path,
            reference=reference,
            candidate=candidate,
            python_version=python_version,
            config=config_path,
            manifest=manifest_path,
            report_dir=effective_report_dir,
            lanes=parse_lane_options(lane or ()),
            force=force,
            invocation_cwd=invocation,
        )
    except FileExistsError as exc:
        if generated_manifest:
            absolute_manifest.unlink(missing_ok=True)
        _fail(str(exc))
    except (ConfigError, MigrationConfigError, WorkspaceError) as exc:
        if generated_manifest:
            absolute_manifest.unlink(missing_ok=True)
        _fail(str(exc))
    except OSError as exc:
        if generated_manifest:
            absolute_manifest.unlink(missing_ok=True)
        _fail(f"migration workspace could not be written ({type(exc).__name__})")
    _print_path_status("created", created)
    if generated_manifest:
        _print_path_status("created starter ledger; review", absolute_manifest)
    console.print(
        "active pair declared; setup will validate candidate packaging without modifying it"
    )
    console.print(f"next: parity migration run --workspace {created}", markup=False)


@migration_app.command("advance")
def migration_advance(
    reference: Annotated[
        str,
        typer.Option(
            "--reference",
            help="New exact released baseline, using the same distribution name",
        ),
    ],
    workspace_path: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Path to parity.workspace.toml"),
    ] = Path("migrations/parity.workspace.toml"),
) -> None:
    """Move the active adjacent pair to a newer released baseline."""

    from parity.migration_workspace import WorkspaceError, advance_workspace

    try:
        advanced = advance_workspace(workspace_path, reference=reference)
    except WorkspaceError as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"migration workspace could not be advanced ({type(exc).__name__})")
    _print_path_status("advanced", advanced)
    console.print("previous active lane reports were invalidated")
    console.print(f"next: parity migration run --workspace {advanced}", markup=False)


@migration_app.command("setup")
def migration_setup(
    workspace_path: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Path to parity.workspace.toml"),
    ] = Path("migrations/parity.workspace.toml"),
    refresh_locks: Annotated[
        bool,
        typer.Option("--refresh-locks", help="Upgrade dependency locks deliberately"),
    ] = False,
) -> None:
    """Prepare all locked reference and candidate worker environments."""

    from parity.migration_workspace import WorkspaceError, setup_workspace

    console.print("[cyan]preparing[/cyan] locked migration environments")
    try:
        prepared = setup_workspace(workspace_path, refresh_locks=refresh_locks)
    except WorkspaceError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"migration environment setup failed ({type(exc).__name__})")
    console.print(f"[green]ready[/green] {len(prepared.lanes)} dependency lane(s)")
    for prepared_lane in prepared.lanes:
        console.print(
            f"  {prepared_lane.name}: {prepared_lane.reference_env}, {prepared_lane.candidate_env}"
        )


@migration_app.command("run")
def migration_run(
    workspace_path: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Path to parity.workspace.toml"),
    ] = Path("migrations/parity.workspace.toml"),
    refresh_locks: Annotated[
        bool,
        typer.Option("--refresh-locks", help="Upgrade dependency locks deliberately"),
    ] = False,
) -> None:
    """Prepare environments and run the complete gate in every lane."""

    from parity.migration import MigrationConfigError
    from parity.migration_workspace import WorkspaceError, run_workspace

    def show_progress(event: str, lane_name: str | None) -> None:
        if event == "setup":
            console.print("[cyan]preparing[/cyan] locked migration environments")
        elif event == "lane" and lane_name is not None:
            console.print(f"[cyan]running[/cyan] dependency lane {lane_name}")
        elif event == "complete" and lane_name is not None:
            console.print(f"[green]completed[/green] dependency lane {lane_name}")

    try:
        completed = run_workspace(
            workspace_path,
            refresh_locks=refresh_locks,
            progress=show_progress,
        )
    except (ConfigError, MigrationConfigError, WorkspaceError) as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"migration workspace could not run ({type(exc).__name__})")

    for index, lane_result in enumerate(completed.lanes):
        if index:
            console.print()
        console.print(Text(f"dependency lane: {lane_result.name}", style="bold"))
        _render_migration_result(lane_result.result)
        _print_path_status("report", Path(lane_result.report.name))
    if any(lane.result.status is Status.ERROR for lane in completed.lanes):
        raise typer.Exit(2)
    if any(lane.result.status is Status.FAILED for lane in completed.lanes):
        raise typer.Exit(1)


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
            written = write_migration_json(result, json_output)
        except OSError as exc:
            _fail(f"migration report could not be written ({type(exc).__name__})")
        _print_path_status("wrote", Path(written.name))
    _render_migration_result(result)
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


def _render_evidence_result(result: object) -> None:
    """Render data-safe retained-evidence status without Rich markup injection."""

    from parity.evidence import EvidenceResult, evidence_summary

    if not isinstance(result, EvidenceResult):  # pragma: no cover - internal contract
        raise TypeError("invalid evidence result")
    colors = {"verified": "green", "stale": "yellow", "error": "red"}
    table = Table("Case", "Artifact", "Status", "Reason")
    for artifact in result.artifacts:
        status = artifact.status.value
        table.add_row(
            Text(artifact.case),
            Text(artifact.artifact),
            Text(status, style=colors[status]),
            Text(artifact.reason_code.value if artifact.reason_code is not None else "—"),
        )
    console.print(table)
    summary = evidence_summary(result)
    console.print(
        " · ".join(
            [
                f"{summary['verified']} verified",
                f"{summary['stale']} stale",
                f"{summary['error']} error",
            ]
        )
    )


@evidence_app.command("verify")
def evidence_verify(
    report: Annotated[Path, typer.Argument(help="Suite or migration JSON report")],
    artifact_root: Annotated[
        Path | None,
        typer.Option(
            "--artifact-root",
            help="Actual artifact_dir when it is not directly below the current directory",
        ),
    ] = None,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write the data-safe verification report"),
    ] = None,
) -> None:
    """Replay every report-referenced finding under its recorded runtime."""

    from parity.evidence import EvidenceError, verify_evidence, write_evidence_json

    try:
        result = verify_evidence(report, artifact_root=artifact_root)
    except EvidenceError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"evidence verification could not run ({type(exc).__name__})")
    if json_output is not None:
        try:
            written = write_evidence_json(result, json_output)
        except OSError as exc:
            _fail(f"evidence report could not be written ({type(exc).__name__})")
        _print_path_status("wrote", Path(written.name))
    _render_evidence_result(result)
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
        version = worker.parity_version or "unavailable"
        return version if worker.parity_satisfied is not False else f"{version} (incompatible)"
    distributions = {item.name: item for item in worker.distributions}
    item = distributions.get(field)
    if item is None:
        return "not requested"
    observed = (
        item.version if item.status == "installed" and item.version is not None else item.status
    )
    if item.satisfied is False:
        requirement = item.requirement or "a valid PEP 440 version"
        return f"{observed} (requires {requirement})"
    return observed


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
        console.print("[green]worker runtimes ready[/green]; targets were not imported")
    else:
        console.print("[red]configured workers not ready[/red]")


if __name__ == "__main__":  # pragma: no cover
    app()
