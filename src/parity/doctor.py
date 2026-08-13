"""Environment diagnostics used by the CLI and support bundles."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    name: str
    installed: bool
    version: str | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    python: str
    executable: str
    platform: str
    working_directory: str
    dependencies: tuple[DependencyStatus, ...]

    @property
    def healthy(self) -> bool:
        return all(dependency.installed for dependency in self.dependencies)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REQUIRED_DEPENDENCIES = (
    "hypothesis",
    "numpy",
    "pandas",
    "polars",
    "psutil",
    "pyarrow",
    "pydantic",
    "rich",
    "typer",
)


def dependency_status(name: str) -> DependencyStatus:
    try:
        importlib.import_module(name)
        version = importlib.metadata.version(name)
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        return DependencyStatus(name=name, installed=False, version=None, detail=str(exc))
    return DependencyStatus(name=name, installed=True, version=version)


def diagnose() -> DoctorReport:
    """Return deterministic diagnostics without exposing environment variables."""

    return DoctorReport(
        python=platform.python_version(),
        executable=sys.executable,
        platform=platform.platform(),
        working_directory=str(Path.cwd()),
        dependencies=tuple(dependency_status(name) for name in REQUIRED_DEPENDENCIES),
    )
