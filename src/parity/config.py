"""TOML configuration loading with location-independent paths."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from parity.models import ParityConfig
from parity.provenance import normalize_distribution_requirements


class ConfigError(ValueError):
    """Raised when a Parity configuration cannot be loaded."""


class _StrictDocumentModel(BaseModel):
    """Strict loader-only syntax that expands into :class:`ParityConfig`."""

    model_config = ConfigDict(extra="forbid")


class _CallableDefaults(_StrictDocumentModel):
    """Reusable non-target fields for one side of a configured case."""

    adapter: Literal["auto", "pandas", "polars", "arrow"] | None = None
    pandas_input: Literal["arrow", "native"] | None = None
    python: Path | None = None
    workdir: Path | None = None
    environment: dict[str, str] | None = None
    record_distributions: list[str] | None = None
    required_distributions: dict[str, str] | None = None


class _ComparisonDefaults(_StrictDocumentModel):
    """Partial comparison policy applied before effective validation."""

    column_order: Literal["strict", "ignore"] | None = None
    row_order: Literal["strict", "ignore", "keyed"] | None = None
    row_keys: list[str] | None = None
    dtype: Literal["strict", "compatible", "ignore"] | None = None
    names: Literal["strict", "case_insensitive"] | None = None
    null_equal: bool | None = None
    nan_equal: bool | None = None
    null_nan_equal: bool | None = None
    signed_zero_equal: bool | None = None
    check_exceptions: bool | None = None
    check_input_mutation: bool | None = None
    rtol: float | None = None
    atol: float | None = None
    datetime_tolerance_ns: int | None = None
    ignored_columns: list[str] | None = None


class _GenerationDefaults(_StrictDocumentModel):
    """Partial generation policy applied before effective validation."""

    max_examples: int | None = None
    max_findings: int | None = None
    stability_repeats: int | None = None
    search: bool | None = None
    seed: int | None = None
    deadline_ms: int | None = None
    adversarial_examples: bool | None = None
    shrink: bool | None = None
    derandomize: bool | None = None
    suppress_too_slow: bool | None = None


class _PerformanceDefaults(_StrictDocumentModel):
    """Partial benchmark policy applied before effective validation."""

    enabled: bool | None = None
    warmups: int | None = None
    repeats: int | None = None
    max_slowdown: float | None = None
    max_memory_ratio: float | None = None
    min_reference_ms: float | None = None
    fail_on_regression: bool | None = None


class _CaseDefaults(_StrictDocumentModel):
    """Bounded defaults that cannot hide case identity, inputs, or targets."""

    reference: _CallableDefaults | None = None
    candidate: _CallableDefaults | None = None
    comparison: _ComparisonDefaults | None = None
    generation: _GenerationDefaults | None = None
    performance: _PerformanceDefaults | None = None
    reference_kwargs: dict[str, Any] | None = None
    candidate_kwargs: dict[str, Any] | None = None
    timeout_seconds: float | None = None


class _ConfigDocument(_StrictDocumentModel):
    """Root TOML syntax before reusable cases are expanded."""

    version: Literal[1] = 1
    artifact_dir: Path = Path(".parity")
    cases: list[dict[str, Any]] | None = None
    cases_file: Path | None = None
    case_defaults: _CaseDefaults | None = None
    fail_fast: bool = False

    @model_validator(mode="after")
    def require_one_case_source(self) -> _ConfigDocument:
        if (self.cases is None) == (self.cases_file is None):
            raise ValueError("exactly one of cases or cases_file is required")
        return self


class _CasesDocument(_StrictDocumentModel):
    """Non-recursive file containing only reusable case declarations."""

    version: Literal[1]
    cases: list[dict[str, Any]] = Field(min_length=1)


def _resolve_paths(config: ParityConfig, base: Path) -> ParityConfig:
    config._base_directory = base.resolve()
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
                # Interpreter launch paths often end in a virtual-environment
                # symlink. Keep that path identity so two venvs pointing at the
                # same base executable can still carry different site-packages.
                implementation.python = Path(os.path.abspath(base / implementation.python))
            if implementation.workdir is not None:
                implementation.workdir = (base / implementation.workdir).resolve()
            else:
                implementation.workdir = base.resolve()
    return config


def _merge_mappings(
    defaults: Mapping[str, Any],
    declared: Mapping[str, Any],
) -> dict[str, Any]:
    """Deep-merge tables while replacing scalar and list fields wholesale."""

    merged = deepcopy(dict(defaults))
    for key, value in declared.items():
        inherited = merged.get(key)
        if isinstance(inherited, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mappings(inherited, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _canonicalize_required_distributions(case: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize callable requirement keys before defaults are deep-merged."""

    canonical = deepcopy(dict(case))
    for side in ("reference", "candidate"):
        callable_spec = canonical.get(side)
        if not isinstance(callable_spec, Mapping):
            continue
        normalized_spec = dict(callable_spec)
        requirements = normalized_spec.get("required_distributions")
        if isinstance(requirements, Mapping):
            try:
                normalized_spec["required_distributions"] = normalize_distribution_requirements(
                    requirements
                )
            except (TypeError, ValueError) as error:
                raise ValueError(str(error)) from error
        canonical[side] = normalized_spec
    return canonical


def _resolve_cases_file(config_path: Path, declared: Path) -> Path:
    """Resolve one non-recursive cases file contained by the root config directory."""

    if declared.is_absolute():
        raise ConfigError("cases_file must be a relative path")
    base = config_path.parent.resolve()
    cases_path = (base / declared).resolve()
    try:
        cases_path.relative_to(base)
    except ValueError as exc:
        raise ConfigError("cases_file must stay within the configuration directory") from exc
    if not cases_path.is_file():
        raise ConfigError(f"cases file not found: {cases_path}")
    return cases_path


def _read_cases_file(config_path: Path, declared: Path) -> list[dict[str, Any]]:
    cases_path = _resolve_cases_file(config_path, declared)
    try:
        raw: dict[str, Any] = tomllib.loads(cases_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in cases file {cases_path}: {exc}") from exc
    try:
        return _CasesDocument.model_validate(raw).cases
    except ValueError as exc:
        raise ConfigError(f"invalid Parity cases file: {exc}") from exc


def _expand_document(document: _ConfigDocument, config_path: Path) -> dict[str, Any]:
    if document.cases_file is not None:
        cases = _read_cases_file(config_path, document.cases_file)
    else:
        assert document.cases is not None  # enforced by _ConfigDocument
        cases = document.cases

    defaults: dict[str, Any] = {}
    if document.case_defaults is not None:
        defaults = _canonicalize_required_distributions(
            document.case_defaults.model_dump(mode="python", exclude_none=True)
        )
    return {
        "version": document.version,
        "artifact_dir": document.artifact_dir,
        "fail_fast": document.fail_fast,
        "cases": [
            _merge_mappings(defaults, _canonicalize_required_distributions(case)) for case in cases
        ],
    }


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
        document = _ConfigDocument.model_validate(raw)
        expanded = _expand_document(document, config_path)
        return _resolve_paths(ParityConfig.model_validate(expanded), config_path.parent)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(f"invalid Parity configuration: {exc}") from exc
