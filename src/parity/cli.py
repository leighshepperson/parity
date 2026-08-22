"""Parity command-line interface."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

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
adapter_app = typer.Typer(
    help="Create and serve supported adapters for external command targets.",
    no_args_is_help=True,
)
contract_app = typer.Typer(
    help="Distill findings and verify candidates without running the reference.",
    no_args_is_help=True,
)
budget_app = typer.Typer(
    help="Capture and review intentional compatibility differences.",
    no_args_is_help=True,
)
app.add_typer(migration_app, name="migration")
app.add_typer(evidence_app, name="evidence")
app.add_typer(adapter_app, name="adapter")
app.add_typer(contract_app, name="contract")
app.add_typer(budget_app, name="budget")
console = Console()
error_console = Console(stderr=True)


def _inspect_generated_adapter(
    path: Path,
) -> tuple[Literal["implemented", "invalid", "placeholder"], str | None]:
    """Inspect generated adapter syntax and recognize its exact executable sentinel."""

    from parity.templates import MIGRATION_ADAPTER_PLACEHOLDER_MESSAGE

    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno is not None else "unknown line"
        return "invalid", f"{exc.msg} ({location})"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        exception = node.exc
        if (
            isinstance(exception.func, ast.Name)
            and exception.func.id == "NotImplementedError"
            and len(exception.args) == 1
            and not exception.keywords
            and isinstance(exception.args[0], ast.Constant)
            and exception.args[0].value == MIGRATION_ADAPTER_PLACEHOLDER_MESSAGE
        ):
            return "placeholder", None
    return "implemented", None


def _checklist_adapter_paths(
    checklist: Any,
    *,
    checklist_path: Path,
    workspace_root: Path,
) -> tuple[Path, ...]:
    """Resolve adapter-review files without allowing out-of-project reads."""

    from parity.agent_output import ChecklistItemId

    item = next(entry for entry in checklist.items if entry.id is ChecklistItemId.ADAPTER_SEMANTICS)
    root = workspace_root.resolve()
    paths: list[Path] = []
    for declared in item.files:
        relative = Path(declared)
        if relative.is_absolute():
            raise ValueError("checklist adapter paths must be relative to the checklist")
        resolved = (checklist_path.parent / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "checklist adapter paths must stay inside the workspace project"
            ) from exc
        paths.append(resolved)
    return tuple(paths)


def _fail(message: str, code: int = 2) -> None:
    rendered = Text()
    rendered.append("error:", style="bold red")
    rendered.append(f" {message}")
    error_console.print(rendered)
    raise typer.Exit(code)


def _cli_path(path: str | Path, *, base: Path | None = None) -> str:
    """Return a stable slash-separated path relative to the invocation when possible."""

    source = Path(path)
    root = (base or Path.cwd()).resolve()
    absolute = source.resolve() if source.is_absolute() else (root / source).resolve()
    try:
        return absolute.relative_to(root).as_posix() or "."
    except ValueError:
        return Path(os.path.relpath(absolute, root)).as_posix()


def _written_path(path: str | Path) -> Path:
    """Show a usable output path without exposing directories above the invocation."""

    source = Path(path)
    root = Path.cwd().resolve()
    absolute = source.resolve() if source.is_absolute() else (root / source).resolve()
    try:
        return Path(absolute.relative_to(root).as_posix())
    except ValueError:
        return Path(absolute.name)


def _artifact_has_executable_replay(path: Path) -> bool:
    """Return whether a freshly written v2 artifact advertises exact replay."""

    try:
        payload = json.loads((path / "replay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or type(payload.get("version")) is not int:
        return False
    path_base = payload.get("path_base")
    levels = path_base.get("levels") if isinstance(path_base, dict) else None
    return bool(
        payload["version"] == 2
        and isinstance(path_base, dict)
        and set(path_base) == {"kind", "levels"}
        and path_base.get("kind") == "artifact_ancestor"
        and type(levels) is int
        and 1 <= levels <= 64
        and payload.get("command") == ["parity", "replay", "<artifact-path>"]
        and "replay_blockers" not in payload
    )


def _agent_document(
    command: str,
    status: str,
    *,
    created_files: list[dict[str, str]] | None = None,
    reports: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    checks: list[dict[str, Any]] | None = None,
    issues: list[dict[str, str]] | None = None,
    next_commands: list[dict[str, Any]] | None = None,
    result: Any = None,
) -> dict[str, Any]:
    """Build the one stable, data-safe result envelope shared by agent-facing commands."""

    return {
        "schema_version": 1,
        "command": command,
        "status": status,
        "created_files": created_files or [],
        "reports": reports or [],
        "artifacts": artifacts or [],
        "checks": checks or [],
        "issues": issues or [],
        "next_commands": next_commands or [],
        "result": result,
    }


def _emit_agent_document(document: dict[str, Any]) -> None:
    """Write exactly one deterministic JSON document to stdout."""

    from parity.agent_output import AgentCommandOutput

    validated = AgentCommandOutput.model_validate(document)
    typer.echo(validated.model_dump_json())


def _agent_fail(command: str, message: str, *, code: int = 2) -> None:
    _emit_agent_document(
        _agent_document(
            command,
            "error",
            issues=[{"code": "operational_error", "severity": "error", "message": message}],
        )
    )
    raise typer.Exit(code)


def _print_path_status(label: str, path: Path, *, style: str = "green") -> None:
    """Render a user-selected path without interpreting Rich markup."""

    rendered = Text()
    rendered.append(label, style=style)
    rendered.append(f" {path}")
    console.print(rendered)


def _version_option_callback(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_option_callback,
            is_eager=True,
            help="Print the installed Parity version and exit",
        ),
    ] = False,
) -> None:
    """Verify that a changed computation still means the same thing."""


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


@adapter_app.command("init")
def adapter_init(
    path: Annotated[
        Path,
        typer.Argument(help="Python adapter module to create"),
    ] = Path("target_adapter.py"),
    program: Annotated[
        str,
        typer.Option("--program", help="Target executable path relative to the adapter"),
    ] = "bin/legacy-target",
    runtime_name: Annotated[
        str,
        typer.Option("--runtime", help="Stable name of the wrapped runtime"),
    ] = "legacy-target",
    runtime_version: Annotated[
        str,
        typer.Option("--runtime-version", help="Stable version of the wrapped runtime"),
    ] = "1.0",
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing adapter file"),
    ] = False,
) -> None:
    """Create one deliberately incomplete, reviewable command adapter."""

    from parity.templates import write_target_adapter

    try:
        created = write_target_adapter(
            path,
            force=force,
            program=program,
            runtime_name=runtime_name,
            runtime_version=runtime_version,
        )
    except (FileExistsError, OSError, TypeError, ValueError) as exc:
        _fail(str(exc))
    shown = _written_path(created)
    command_path = _cli_path(created)
    _print_path_status("created", shown)
    typer.echo("add this endpoint to parity.toml:")
    typer.echo("[cases.reference]")
    typer.echo(
        "command = " + json.dumps(["parity", "adapter", "serve", command_path], ensure_ascii=False)
    )
    typer.echo("next: parity doctor --config parity.toml")


def _load_command_adapter(path: Path) -> Any:
    """Load one explicit regular Python file and return its exported adapter."""

    source = path if path.is_absolute() else Path.cwd() / path
    try:
        metadata = source.lstat()
    except OSError:
        raise ValueError("adapter module is unavailable") from None
    if (
        source.suffix != ".py"
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or source.is_symlink()
    ):
        raise ValueError("adapter module must be a single-linked regular Python file")
    source = source.resolve(strict=True)
    specification = importlib.util.spec_from_file_location("_parity_command_adapter", source)
    if specification is None or specification.loader is None:
        raise ValueError("adapter module could not be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    parent = str(source.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(specification.name, None)
        raise ValueError("adapter module could not be loaded") from None
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode

    from parity.target_adapter import CommandAdapter

    adapter = getattr(module, "adapter", None)
    if not isinstance(adapter, CommandAdapter):
        raise ValueError("adapter module must export one CommandAdapter as 'adapter'")
    return adapter


@adapter_app.command("serve")
def adapter_serve(
    path: Annotated[Path, typer.Argument(help="Python adapter module")],
    session_root: Annotated[Path, typer.Argument(help="Private protocol session root")],
) -> None:
    """Serve one generated adapter through target protocol v1."""

    try:
        adapter = _load_command_adapter(path)
    except (OSError, TypeError, ValueError):
        raise typer.Exit(1) from None
    adapter.serve(session_root)


@app.command("schema")
def schema_command(
    name: Annotated[
        str,
        typer.Argument(help="Contract name, or 'list' to enumerate contracts"),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the schema atomically to this path"),
    ] = None,
) -> None:
    """Print a versioned JSON Schema for an authored or emitted contract."""

    from parity.json_contracts import contract_names, contract_schema

    if name == "list":
        rendered = json.dumps(
            {"schema_version": 1, "contracts": list(contract_names())},
            indent=2,
            sort_keys=True,
        )
    else:
        try:
            schema = contract_schema(name)
        except ValueError as exc:
            _fail(str(exc))
        rendered = json.dumps(schema, indent=2, sort_keys=True, allow_nan=False)
    if output is None:
        typer.echo(rendered)
        return
    try:
        _atomic_write_output(output, rendered + "\n")
    except OSError as exc:
        _fail(f"schema output could not be written ({type(exc).__name__})")
    _print_path_status("wrote", output)


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
    config_argument = path.as_posix()
    next_argv = ["parity", "check"]
    if config_argument != "parity.toml":
        next_argv.extend(["--config", config_argument])
    typer.echo(f"next: {shlex.join(next_argv)}")


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
    jobs: Annotated[
        int | None,
        typer.Option("--jobs", "-j", min=1, max=256, help="Run independent cases concurrently"),
    ] = None,
    native_threads: Annotated[
        int | None,
        typer.Option(
            "--native-threads",
            min=1,
            max=256,
            help="Limit common BLAS/OpenMP thread pools in each worker",
        ),
    ] = None,
    performance: Annotated[
        bool | None,
        typer.Option(
            "--performance/--no-performance",
            help="Override configured performance checks",
        ),
    ] = None,
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
        if performance is not None:
            item.performance.enabled = performance

    try:
        if jobs is not None:
            config.jobs = jobs
        if native_threads is not None:
            config.native_threads = native_threads
    except ValueError as exc:
        _fail(str(exc))

    try:
        result = run_suite(config, selected_cases=selected or None)
    except ValueError as exc:
        _fail(str(exc))
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
        _print_path_status("wrote", _written_path(path))
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
        render_terminal(
            result.suite,
            console=console,
            artifact_renderer=lambda artifact: _cli_path(artifact),
        )


@migration_app.command("init")
def migration_init(
    reference_package: Annotated[
        str | None,
        typer.Option(
            "--reference-package",
            help="Exact released requirement, for example package==1.2.3",
        ),
    ] = None,
    reference_path: Annotated[
        Path | None,
        typer.Option(
            "--reference-path",
            help=(
                "Existing local reference checkout (mutually exclusive with --reference-package)"
            ),
        ),
    ] = None,
    workspace_path: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Workspace file to create"),
    ] = Path("migrations/parity.workspace.toml"),
    candidate_package: Annotated[
        str | None,
        typer.Option(
            "--candidate-package",
            help="Exact released requirement, for example package==2.0.0",
        ),
    ] = None,
    candidate_path: Annotated[
        Path | None,
        typer.Option(
            "--candidate-path",
            help="Existing local candidate checkout; defaults to the current directory",
        ),
    ] = None,
    python_version: Annotated[
        str | None,
        typer.Option(
            "--python",
            help="Shared target Python major.minor; defaults to this Python",
        ),
    ] = None,
    reference_python_version: Annotated[
        str | None,
        typer.Option(
            "--reference-python",
            help="Reference target Python major.minor; overrides --python",
        ),
    ] = None,
    candidate_python_version: Annotated[
        str | None,
        typer.Option(
            "--candidate-python",
            help="Candidate target Python major.minor; overrides --python",
        ),
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
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Import target used by both sides when scaffolding a new config",
        ),
    ] = None,
    reference_target: Annotated[
        str | None,
        typer.Option(
            "--reference-target",
            help="Reference import target; overrides --target",
        ),
    ] = None,
    candidate_target: Annotated[
        str | None,
        typer.Option(
            "--candidate-target",
            help="Candidate import target; overrides --target",
        ),
    ] = None,
    fixture: Annotated[
        Path | None,
        typer.Option("--fixture", help="Fixture for a newly scaffolded migration case"),
    ] = None,
    case_name: Annotated[
        str,
        typer.Option("--case-name", help="Name for a newly scaffolded migration case"),
    ] = "migration",
    reference_adapter: Annotated[
        str,
        typer.Option("--reference-adapter", help="auto, pandas, polars or arrow"),
    ] = "auto",
    candidate_adapter: Annotated[
        str,
        typer.Option("--candidate-adapter", help="auto, pandas, polars or arrow"),
    ] = "auto",
    record_distribution: Annotated[
        list[str] | None,
        typer.Option(
            "--record-distribution",
            help="Additional distribution version to record; repeatable",
        ),
    ] = None,
    row_key: Annotated[
        list[str] | None,
        typer.Option("--row-key", help="Unique output key column; repeatable"),
    ] = None,
    scaffold: Annotated[
        bool,
        typer.Option(
            "--scaffold",
            help="Create a safe adapter, fixture and explicit review checklist",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one machine-readable result document"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing workspace file"),
    ] = False,
) -> None:
    """Declare a managed migration and optionally scaffold its first case."""

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
    from parity.templates import write_migration_scaffold, write_project_config

    def fail(message: str) -> None:
        if as_json:
            _agent_fail("migration.init", message)
        _fail(message)

    invocation = Path.cwd()
    if (reference_package is None) == (reference_path is None):
        fail("set exactly one of --reference-package or --reference-path")
    if candidate_package is not None and candidate_path is not None:
        fail("set exactly one of --candidate-package or --candidate-path")
    if candidate_package is None and candidate_path is None:
        candidate_path = Path(".")
    absolute_workspace = (
        workspace_path if workspace_path.is_absolute() else invocation / workspace_path
    )
    absolute_config = config_path if config_path.is_absolute() else invocation / config_path
    absolute_manifest = manifest_path if manifest_path.is_absolute() else invocation / manifest_path
    if os.path.lexists(absolute_workspace) and not force:
        fail(f"{workspace_path} already exists; pass --force to replace it")

    scaffold_requested = any(
        (
            target is not None,
            reference_target is not None,
            candidate_target is not None,
            fixture is not None,
            case_name != "migration",
            reference_adapter != "auto",
            candidate_adapter != "auto",
            bool(record_distribution),
            bool(row_key),
        )
    )
    if scaffold and scaffold_requested:
        fail(
            "--scaffold creates the adapter and fixture; omit --target, --fixture and other "
            "case-scaffolding options"
        )
    generated_config = False
    generated_manifest = False
    generated_scaffold: dict[str, Path] = {}
    checklist_path: Path | None = None
    try:
        if os.path.lexists(absolute_config):
            if scaffold or scaffold_requested:
                raise WorkspaceError(
                    "the reviewed Parity config already exists; omit --target, --fixture and "
                    "other case-scaffolding options"
                )
            configured = load_config(absolute_config)
        else:
            if scaffold:
                generated_scaffold = write_migration_scaffold(
                    absolute_config,
                    manifest_path=absolute_manifest,
                    case_name=case_name,
                )
                checklist_path = generated_scaffold["checklist"]
            else:
                effective_reference_target = reference_target or target
                effective_candidate_target = candidate_target or target
                if (
                    fixture is None
                    or effective_reference_target is None
                    or effective_candidate_target is None
                ):
                    raise WorkspaceError(
                        "the Parity config is missing; pass --scaffold, or supply --fixture and "
                        "either --target or both --reference-target and --candidate-target"
                    )
                absolute_fixture = fixture if fixture.is_absolute() else invocation / fixture
                write_project_config(
                    absolute_config,
                    reference=effective_reference_target,
                    candidate=effective_candidate_target,
                    fixture=absolute_fixture,
                    case_name=case_name,
                    reference_adapter=reference_adapter,
                    candidate_adapter=candidate_adapter,
                    target_workdir=absolute_workspace.parent,
                    record_distributions=record_distribution or (),
                    row_keys=row_key or (),
                )
            generated_config = True
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
            reference_package=reference_package,
            reference_path=reference_path,
            candidate_package=candidate_package,
            candidate_path=candidate_path,
            python_version=python_version,
            reference_python_version=reference_python_version,
            candidate_python_version=candidate_python_version,
            config=config_path,
            manifest=manifest_path,
            checklist=checklist_path,
            report_dir=effective_report_dir,
            lanes=parse_lane_options(lane or ()),
            force=force,
            invocation_cwd=invocation,
        )
    except FileExistsError as exc:
        if generated_manifest:
            absolute_manifest.unlink(missing_ok=True)
        if generated_scaffold:
            for path in generated_scaffold.values():
                path.unlink(missing_ok=True)
        elif generated_config:
            absolute_config.unlink(missing_ok=True)
        fail(str(exc))
    except (ConfigError, MigrationConfigError, WorkspaceError, ValueError) as exc:
        if generated_manifest:
            absolute_manifest.unlink(missing_ok=True)
        if generated_scaffold:
            for path in generated_scaffold.values():
                path.unlink(missing_ok=True)
        elif generated_config:
            absolute_config.unlink(missing_ok=True)
        fail(str(exc))
    except OSError as exc:
        if generated_manifest:
            absolute_manifest.unlink(missing_ok=True)
        if generated_scaffold:
            for path in generated_scaffold.values():
                path.unlink(missing_ok=True)
        elif generated_config:
            absolute_config.unlink(missing_ok=True)
        fail(f"migration workspace could not be written ({type(exc).__name__})")
    if as_json:
        created_files: list[dict[str, str]] = [
            {"kind": "workspace", "path": _cli_path(created, base=invocation)}
        ]
        if generated_config:
            created_files.append(
                {"kind": "config", "path": _cli_path(absolute_config, base=invocation)}
            )
        if generated_manifest:
            created_files.append(
                {"kind": "manifest", "path": _cli_path(absolute_manifest, base=invocation)}
            )
        for kind in ("adapter", "fixture", "checklist"):
            if scaffold_path := generated_scaffold.get(kind):
                created_files.append(
                    {"kind": kind, "path": _cli_path(scaffold_path, base=invocation)}
                )
        issues = (
            [
                {
                    "code": "contract_review_required",
                    "severity": "review",
                    "message": "resolve every migration checklist item before execution",
                }
            ]
            if scaffold
            else []
        )
        _emit_agent_document(
            _agent_document(
                "migration.init",
                "needs_review" if scaffold else "ready",
                created_files=created_files,
                issues=issues,
                next_commands=[
                    {
                        "argv": [
                            "parity",
                            "migration",
                            "validate",
                            "--workspace",
                            _cli_path(created, base=invocation),
                            "--json",
                        ],
                        "cwd": "invocation",
                    }
                ],
            )
        )
        return
    _print_path_status("created", created)
    if generated_config:
        _print_path_status("created migration contract; review", absolute_config)
    if generated_manifest:
        _print_path_status("created starter ledger; review", absolute_manifest)
    if reference_package is not None and candidate_package is not None:
        console.print("released pair declared; setup will lock and verify both exact versions")
    elif reference_path is not None and candidate_path is not None:
        console.print(
            "local pair declared; setup will verify both editable installs and source revisions"
        )
    else:
        console.print(
            "active pair declared (mixed sources); setup will install the local checkout "
            "without modifying it"
        )
    console.print(f"next: parity migration validate --workspace {created}", markup=False)


@migration_app.command("validate")
def migration_validate(
    workspace_path: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Path to parity.workspace.toml"),
    ] = Path("migrations/parity.workspace.toml"),
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one machine-readable result document"),
    ] = False,
) -> None:
    """Validate authored contracts without creating environments or invoking targets."""

    from parity.adapters import load_arrow_fixture
    from parity.agent_output import ContractChecklist
    from parity.migration import MigrationConfigError, load_migration_manifest
    from parity.migration_workspace import WorkspaceError, load_workspace

    invocation = Path.cwd()
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    try:
        workspace = load_workspace(workspace_path)
        checks.append(
            {
                "code": "workspace_valid",
                "status": "passed",
                "message": "workspace v3 is structurally valid",
                "path": _cli_path(workspace.path, base=invocation),
            }
        )
        configured = load_config(workspace.config)
        checks.append(
            {
                "code": "config_valid",
                "status": "passed",
                "message": "Parity configuration is structurally valid",
                "path": _cli_path(workspace.config, base=invocation),
            }
        )
        manifest = load_migration_manifest(workspace.manifest)
        checks.append(
            {
                "code": "manifest_valid",
                "status": "passed",
                "message": "migration ledger is structurally valid",
                "path": _cli_path(workspace.manifest, base=invocation),
            }
        )

        fixture_count = 0
        for case in configured.cases:
            fixtures = [case.fixture] if case.fixture is not None else []
            if case.input_bundle is not None:
                fixtures.extend(
                    spec.fixture
                    for spec in case.input_bundle.inputs.values()
                    if spec.fixture is not None
                )
            for fixture_path in fixtures:
                assert fixture_path is not None
                load_arrow_fixture(fixture_path)
                fixture_count += 1
        checks.append(
            {
                "code": "fixtures_loadable",
                "status": "passed",
                "message": f"{fixture_count} declared fixture(s) loaded as Arrow",
            }
        )

        selected = {case for unit in manifest.units for case in unit.cases}
        known = {case.name for case in configured.cases}
        if unknown := selected - known:
            raise MigrationConfigError(
                "migration manifest references unknown case(s): " + ", ".join(sorted(unknown))
            )
        uncovered = [
            unit.id for unit in manifest.units if not unit.cases and unit.excluded_reason is None
        ]
        if uncovered:
            for unit_id in uncovered:
                issues.append(
                    {
                        "code": "migration_unit_uncovered",
                        "severity": "review",
                        "message": f"migration unit {unit_id!r} needs cases or an exclusion reason",
                    }
                )
            checks.append(
                {
                    "code": "migration_mapping_complete",
                    "status": "failed",
                    "message": f"{len(uncovered)} migration unit(s) remain uncovered",
                }
            )
        else:
            checks.append(
                {
                    "code": "migration_mapping_complete",
                    "status": "passed",
                    "message": "every migration unit is mapped or explicitly excluded",
                }
            )

        if workspace.checklist is None:
            checks.append(
                {
                    "code": "contract_review",
                    "status": "deferred",
                    "message": "no generated checklist is attached to this authored workspace",
                }
            )
        else:
            checklist = ContractChecklist.model_validate_json(
                workspace.checklist.read_text(encoding="utf-8")
            )
            adapter_states = [
                (path, *_inspect_generated_adapter(path))
                for path in _checklist_adapter_paths(
                    checklist,
                    checklist_path=workspace.checklist,
                    workspace_root=workspace.root,
                )
            ]
            invalid_adapters = [item for item in adapter_states if item[1] == "invalid"]
            placeholder_adapters = [item for item in adapter_states if item[1] == "placeholder"]
            if invalid_adapters:
                adapter_path, _, detail = invalid_adapters[0]
                issues.append(
                    {
                        "code": "adapter_python_invalid",
                        "severity": "error",
                        "message": f"fix invalid Python in the migration adapter: {detail}",
                        "path": _cli_path(adapter_path, base=invocation),
                    }
                )
                checks.append(
                    {
                        "code": "adapter_implemented",
                        "status": "failed",
                        "message": f"the migration adapter contains invalid Python: {detail}",
                        "path": _cli_path(adapter_path, base=invocation),
                    }
                )
            elif placeholder_adapters:
                adapter_path = placeholder_adapters[0][0]
                issues.append(
                    {
                        "code": "generated_adapter_placeholder",
                        "severity": "review",
                        "message": (
                            "replace the generated NotImplementedError with the migration contract"
                        ),
                        "path": _cli_path(adapter_path, base=invocation),
                    }
                )
                checks.append(
                    {
                        "code": "adapter_implemented",
                        "status": "failed",
                        "message": "the generated migration adapter is still a placeholder",
                        "path": _cli_path(adapter_path, base=invocation),
                    }
                )
            else:
                checks.append(
                    {
                        "code": "adapter_implemented",
                        "status": "passed",
                        "message": "the generated migration adapter placeholder was replaced",
                    }
                )
            if checklist.unresolved_ids:
                for identifier in checklist.unresolved_ids:
                    issues.append(
                        {
                            "code": f"checklist.{identifier.value}",
                            "severity": "review",
                            "message": "explicit contract review remains unresolved",
                            "path": _cli_path(workspace.checklist, base=invocation),
                        }
                    )
                checks.append(
                    {
                        "code": "contract_review",
                        "status": "failed",
                        "message": f"{len(checklist.unresolved_ids)} checklist item(s) remain",
                        "path": _cli_path(workspace.checklist, base=invocation),
                    }
                )
            else:
                checks.append(
                    {
                        "code": "contract_review",
                        "status": "passed",
                        "message": "every generated contract decision is marked resolved",
                        "path": _cli_path(workspace.checklist, base=invocation),
                    }
                )
    except (ConfigError, MigrationConfigError, WorkspaceError, OSError, ValueError) as exc:
        if as_json:
            _agent_fail("migration.validate", str(exc))
        _fail(str(exc))

    needs_review = bool(issues)
    next_argv = [
        "parity",
        "migration",
        "validate" if needs_review else "run",
        "--workspace",
        _cli_path(workspace.path, base=invocation),
    ]
    if as_json:
        next_argv.append("--json")
        _emit_agent_document(
            _agent_document(
                "migration.validate",
                "needs_review" if needs_review else "ready",
                checks=checks,
                issues=issues,
                next_commands=[{"argv": next_argv, "cwd": "invocation"}],
            )
        )
    else:
        table = Table("Check", "Status", "Detail")
        for check_result in checks:
            table.add_row(
                str(check_result["code"]),
                str(check_result["status"]),
                str(check_result["message"]),
            )
        console.print(table)
        if needs_review:
            console.print("[yellow]migration contract is not ready[/yellow]")
        else:
            console.print("[green]migration contract is ready to run[/green]")
    if needs_review:
        raise typer.Exit(1)


@migration_app.command("advance")
def migration_advance(
    reference_package: Annotated[
        str,
        typer.Option(
            "--reference-package",
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
        advanced = advance_workspace(workspace_path, reference_package=reference_package)
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
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one machine-readable result document"),
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
            progress=None if as_json else show_progress,
        )
    except (ConfigError, MigrationConfigError, WorkspaceError) as exc:
        if as_json:
            _agent_fail("migration.run", str(exc))
        _fail(str(exc))
    except Exception as exc:
        message = f"migration workspace could not run ({type(exc).__name__})"
        if as_json:
            _agent_fail("migration.run", message)
        _fail(message)

    has_error = any(lane.result.status is Status.ERROR for lane in completed.lanes)
    has_failure = any(lane.result.status is Status.FAILED for lane in completed.lanes)
    if as_json:
        from parity.migration import migration_report_payload

        reports: list[dict[str, Any]] = [
            {
                "kind": "migration",
                "path": _cli_path(lane.report),
                "lane": lane.name,
            }
            for lane in completed.lanes
        ]
        if completed.source_provenance is not None:
            reports.append(
                {
                    "kind": "source_provenance",
                    "path": _cli_path(completed.source_provenance),
                    "lane": None,
                }
            )
        artifact_by_path: dict[str, dict[str, Any]] = {}
        evidence_only_issues: dict[str, dict[str, str]] = {}
        for lane in completed.lanes:
            for case_result in lane.result.suite.cases:
                for failure in case_result.failures:
                    if failure.artifact is None:
                        continue
                    rendered = _cli_path(failure.artifact)
                    artifact_reference: dict[str, Any] = {
                        "path": rendered,
                        "case": case_result.name,
                        "finding_signature": failure.finding_signature,
                    }
                    if _artifact_has_executable_replay(failure.artifact):
                        artifact_reference["replay_command"] = {
                            "argv": ["parity", "replay", rendered, "--json"],
                            "cwd": "invocation",
                        }
                    else:
                        evidence_only_issues.setdefault(
                            rendered,
                            {
                                "code": "artifact.evidence_only",
                                "severity": "warning",
                                "message": (
                                    "artifact is retained evidence but has no executable replay "
                                    "contract"
                                ),
                                "path": rendered,
                                "case": case_result.name,
                            },
                        )
                    artifact_by_path.setdefault(rendered, artifact_reference)
        _emit_agent_document(
            _agent_document(
                "migration.run",
                "error" if has_error else "failed" if has_failure else "passed",
                reports=reports,
                artifacts=list(artifact_by_path.values()),
                issues=list(evidence_only_issues.values()),
                result={
                    "lanes": [
                        {
                            "name": lane.name,
                            "report": migration_report_payload(lane.result),
                        }
                        for lane in completed.lanes
                    ]
                },
            )
        )
        if has_error:
            raise typer.Exit(2)
        if has_failure:
            raise typer.Exit(1)
        return

    for index, lane_result in enumerate(completed.lanes):
        if index:
            console.print()
        console.print(Text(f"dependency lane: {lane_result.name}", style="bold"))
        _render_migration_result(lane_result.result)
        _print_path_status("report", Path(_cli_path(lane_result.report)))
    if completed.source_provenance is not None:
        _print_path_status("source provenance", Path(_cli_path(completed.source_provenance)))
    if has_error:
        raise typer.Exit(2)
    if has_failure:
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
        _print_path_status("wrote", _written_path(written))
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
        _print_path_status("wrote", _written_path(written))
    _render_evidence_result(result)
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


@budget_app.command("init")
def budget_init(
    report: Annotated[Path, typer.Argument(help="Suite or migration JSON report")],
    destination: Annotated[Path, typer.Argument(help="New compatibility budget TOML")],
    force: Annotated[bool, typer.Option("--force", help="Replace an existing budget")] = False,
) -> None:
    """Capture every signed finding into an explicit review ledger."""

    from parity.compatibility import (
        CompatibilityBudgetError,
        capture_compatibility_budget,
        load_compatibility_budget,
    )

    try:
        result = capture_compatibility_budget(report, destination, force=force)
        budget = load_compatibility_budget(result.path)
    except FileExistsError:
        _fail(f"{destination} already exists; pass --force to replace it")
    except CompatibilityBudgetError as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"compatibility budget could not be written ({type(exc).__name__})")
    _print_path_status("created", _written_path(result.path))
    console.print(f"{result.findings} finding(s) require review")
    first = budget.findings[0]
    console.print(
        "next: "
        + shlex.join(
            [
                "parity",
                "budget",
                "approve",
                str(result.path),
                first.case,
                first.finding_signature,
                "--reason",
                "why this difference is acceptable",
            ]
        ),
        markup=False,
    )


@budget_app.command("approve")
def budget_approve(
    budget: Annotated[Path, typer.Argument(help="Compatibility budget TOML")],
    case: Annotated[str, typer.Argument(help="Exact case name")],
    finding_signature: Annotated[str, typer.Argument(help="Exact ms3 finding signature")],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Reviewed rationale for accepting this difference"),
    ],
) -> None:
    """Approve one report-captured finding with a required rationale."""

    from parity.compatibility import CompatibilityBudgetError, approve_compatibility_finding
    from parity.models import CompatibilityDecision

    try:
        updated = approve_compatibility_finding(
            budget,
            case,
            finding_signature,
            reason=reason,
        )
    except CompatibilityBudgetError as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"compatibility budget could not be updated ({type(exc).__name__})")
    _print_path_status("approved", _written_path(budget))
    remaining = [
        finding for finding in updated.findings if finding.decision is CompatibilityDecision.REVIEW
    ]
    if remaining:
        console.print(f"{len(remaining)} finding(s) still require review")
        next_finding = remaining[0]
        console.print(
            "next: "
            + shlex.join(
                [
                    "parity",
                    "budget",
                    "approve",
                    str(budget),
                    next_finding.case,
                    next_finding.finding_signature,
                    "--reason",
                    "why this difference is acceptable",
                ]
            ),
            markup=False,
        )
    else:
        console.print(
            f"next: set compatibility_budget = {json.dumps(_cli_path(budget))} in parity.toml",
            markup=False,
        )


@contract_app.command("distill")
def contract_distill(
    report: Annotated[Path, typer.Argument(help="Suite or migration JSON report")],
    destination: Annotated[Path, typer.Argument(help="New private contract directory")],
    artifact_root: Annotated[
        Path | None,
        typer.Option(
            "--artifact-root",
            help="Actual artifact_dir when it is not directly below the current directory",
        ),
    ] = None,
) -> None:
    """Capture signed findings as a candidate-only semantic contract."""

    from parity.distilled import ContractError, distill_contract

    try:
        result = distill_contract(
            report,
            destination,
            artifact_root=artifact_root,
        )
    except ContractError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"contract could not be distilled ({type(exc).__name__})")
    _print_path_status("wrote", _written_path(result.path))
    console.print(f"{result.examples} example(s) across {result.cases} case(s)")


@contract_app.command("retire")
def contract_retire(
    contract: Annotated[Path, typer.Argument(help="Reference-baseline contract directory")],
    destination: Annotated[Path, typer.Argument(help="New retired contract directory")],
    budget: Annotated[
        Path | None,
        typer.Option("--budget", help="Reviewed compatibility budget for intentional differences"),
    ] = None,
) -> None:
    """Freeze the stable, reviewed candidate as the new reference-free baseline."""

    from parity.distilled import ContractError, retire_contract

    try:
        result = retire_contract(contract, destination, budget=budget)
    except ContractError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"reference could not be retired ({type(exc).__name__})")
    _print_path_status("retired", _written_path(result.path))
    console.print(
        f"{result.examples} stable example(s) across {result.cases} case(s); "
        f"{result.approvals} approved difference(s) promoted"
    )
    console.print(
        "next: " + shlex.join(["parity", "contract", "verify", str(result.path)]),
        markup=False,
    )


@contract_app.command("verify")
def contract_verify(
    contract: Annotated[Path, typer.Argument(help="Distilled contract directory")],
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write the data-safe verification report"),
    ] = None,
) -> None:
    """Verify only the candidate against captured reference expectations."""

    from parity.distilled import ContractError, verify_contract
    from parity.reporting import render_terminal, write_report

    try:
        result = verify_contract(contract)
    except ContractError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"contract verification could not run ({type(exc).__name__})")
    if json_output is not None:
        try:
            written = write_report(result, "json", json_output)
        except (OSError, ValueError) as exc:
            _fail(f"verification report could not be written ({type(exc).__name__})")
        _print_path_status("wrote", _written_path(written))
    render_terminal(result, console=console)
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


@app.command()
def replay(
    artifact: Annotated[Path, typer.Argument(help="Finding artifact directory")],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one machine-readable result document"),
    ] = False,
) -> None:
    """Re-run a preserved counterexample exactly."""

    from parity.engine import replay_artifact
    from parity.reporting import render_terminal

    try:
        result = replay_artifact(artifact)
    except Exception as exc:
        if as_json:
            _agent_fail("replay", str(exc))
        _fail(str(exc))
    if as_json:
        from parity.reporting import report_payload

        _emit_agent_document(
            _agent_document(
                "replay",
                result.status.value,
                artifacts=[{"path": _cli_path(artifact)}],
                result=report_payload(result),
            )
        )
        if result.status is Status.ERROR:
            raise typer.Exit(2)
        if result.status is Status.FAILED:
            raise typer.Exit(1)
        return
    render_terminal(
        result,
        console=console,
        artifact_renderer=lambda path: _cli_path(path),
    )
    if result.status is Status.ERROR:
        raise typer.Exit(2)
    if result.status is Status.FAILED:
        raise typer.Exit(1)


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable output")] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Inspect targets from this parity.toml"),
    ] = None,
    case: Annotated[
        str | None,
        typer.Option("--case", help="Inspect only one configured case"),
    ] = None,
) -> None:
    """Check local dependencies or configured target environments."""

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
        status = worker.status.replace("_", " ")
        return f"{status} ({worker.error_code})" if worker.error_code else status
    if field == "python":
        return f"{worker.python_implementation} {worker.python_version}"
    if field == "parity":
        if worker.executor != "parity-python":
            protocol = (
                "portable protocol v1"
                if worker.executor == "portable-python"
                else "target protocol v1"
            )
            return f"not required ({protocol})"
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
        console.print("[green]target runtimes and imports ready[/green]; targets were not invoked")
    else:
        console.print("[red]configured targets not ready[/red]; reason codes identify the phase")


if __name__ == "__main__":  # pragma: no cover
    app()
