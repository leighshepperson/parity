"""TOML configuration loading with location-independent paths."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from parity.models import ParityConfig


class ConfigError(ValueError):
    """Raised when a Parity configuration cannot be loaded."""


def _resolve_paths(config: ParityConfig, base: Path) -> ParityConfig:
    config.artifact_dir = (base / config.artifact_dir).resolve()
    for case in config.cases:
        if case.fixture is not None:
            case.fixture = (base / case.fixture).resolve()
        if case.input_bundle is not None:
            for input_spec in case.input_bundle.inputs.values():
                if input_spec.fixture is not None:
                    input_spec.fixture = (base / input_spec.fixture).resolve()
        for implementation in (case.reference, case.candidate):
            if implementation.python is not None:
                implementation.python = (base / implementation.python).resolve()
            if implementation.workdir is not None:
                implementation.workdir = (base / implementation.workdir).resolve()
            else:
                implementation.workdir = base.resolve()
    return config


def load_config(path: str | Path = "parity.toml") -> ParityConfig:
    """Load and validate a parity.toml file."""

    config_path = Path(path).resolve()
    try:
        raw: dict[str, Any] = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    try:
        return _resolve_paths(ParityConfig.model_validate(raw), config_path.parent)
    except ValueError as exc:
        raise ConfigError(f"invalid Parity configuration: {exc}") from exc
