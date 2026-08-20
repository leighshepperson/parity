"""Data-safe runtime and effective-configuration provenance.

Runtime provenance is deliberately collected from inside each execution
environment.  It contains only bounded platform labels and versions for a
small, explicit set of Python distributions; it never inspects environment
values, paths, hostnames, command lines, or installed packages in bulk.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from parity._version import __version__

MAX_RECORDED_DISTRIBUTIONS = 64
MAX_DISTRIBUTION_NAME_LENGTH = 128
MAX_DISTRIBUTION_SPECIFIER_LENGTH = 256
CORE_DISTRIBUTIONS = ("hypothesis", "numpy", "pandas", "polars", "pyarrow")

_DISTRIBUTION_NAME = re.compile(
    rf"^[A-Za-z0-9](?:[A-Za-z0-9._-]{{0,{MAX_DISTRIBUTION_NAME_LENGTH - 2}}}"
    r"[A-Za-z0-9])?$"
)
_DISTRIBUTION_SEPARATOR = re.compile(r"[-_.]+")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!~-]{0,127}$")
_SAFE_RUNTIME_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}$")
_SECRET_KEY = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)
_OMIT = object()


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DistributionProvenance(_StrictFrozenModel):
    """Version status for one normalized Python distribution name."""

    name: str = Field(min_length=1, max_length=MAX_DISTRIBUTION_NAME_LENGTH)
    status: Literal["installed", "missing", "unavailable"]
    version: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_normalized_name(cls, name: str) -> str:
        normalized = normalize_distribution_name(name)
        if name != normalized:
            raise ValueError(f"distribution name must be normalized as {normalized!r}")
        return name

    @field_validator("version")
    @classmethod
    def validate_version(cls, version: str | None) -> str | None:
        if version is not None and not _SAFE_VERSION.fullmatch(version):
            raise ValueError("distribution version contains unsupported characters")
        return version

    @model_validator(mode="after")
    def validate_status_and_version(self) -> DistributionProvenance:
        if self.status == "installed" and self.version is None:
            raise ValueError("an installed distribution requires a version")
        if self.status != "installed" and self.version is not None:
            raise ValueError("only an installed distribution may have a version")
        return self


class RuntimeIdentity(_StrictFrozenModel):
    """One optional path-free identity claim supplied by a target runtime."""

    name: str = Field(min_length=1, max_length=MAX_DISTRIBUTION_NAME_LENGTH)
    kind: Literal["git-worktree-v1"]
    revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    dirty: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def validate_normalized_name(cls, name: str) -> str:
        normalized = normalize_distribution_name(name)
        if name != normalized:
            raise ValueError(f"runtime identity name must be normalized as {normalized!r}")
        return name


class RuntimeProvenance(_StrictFrozenModel):
    """Bounded, path-free identity reported by one target executor."""

    executor: Literal["parity-python", "portable-python", "command"] = "parity-python"
    runtime_name: str | None = Field(default=None, min_length=1, max_length=64)
    runtime_version: str | None = Field(default=None, min_length=1, max_length=64)
    python_implementation: str | None = Field(default=None, min_length=1, max_length=64)
    python_version: str | None = Field(default=None, min_length=1, max_length=64)
    platform_system: str = Field(min_length=1, max_length=64)
    platform_machine: str = Field(min_length=1, max_length=64)
    parity_version: str | None = Field(default=None, min_length=1, max_length=128)
    distributions: tuple[DistributionProvenance, ...] = ()
    identities: tuple[RuntimeIdentity, ...] = ()

    @field_validator("platform_system", "platform_machine")
    @classmethod
    def validate_safe_label(cls, label: str) -> str:
        if not _SAFE_RUNTIME_LABEL.fullmatch(label):
            raise ValueError("runtime label contains unsupported characters")
        return label

    @field_validator(
        "runtime_name",
        "runtime_version",
        "python_implementation",
        "python_version",
        "parity_version",
    )
    @classmethod
    def validate_optional_safe_label(cls, label: str | None) -> str | None:
        if label is not None and not _SAFE_RUNTIME_LABEL.fullmatch(label):
            raise ValueError("runtime label contains unsupported characters")
        return label

    @model_validator(mode="after")
    def validate_executor_runtime(self) -> RuntimeProvenance:
        if self.executor in {"parity-python", "portable-python"} and (
            self.python_implementation is None or self.python_version is None
        ):
            raise ValueError("Python executors must report their implementation and version")
        if (self.runtime_name is None) != (self.runtime_version is None):
            raise ValueError("runtime_name and runtime_version must be reported together")
        return self

    @field_validator("distributions")
    @classmethod
    def validate_distributions(
        cls, distributions: tuple[DistributionProvenance, ...]
    ) -> tuple[DistributionProvenance, ...]:
        maximum = len(CORE_DISTRIBUTIONS) + MAX_RECORDED_DISTRIBUTIONS
        if len(distributions) > maximum:
            raise ValueError(f"at most {maximum} runtime distributions may be recorded")
        names = [distribution.name for distribution in distributions]
        if names != sorted(names):
            raise ValueError("runtime distributions must be sorted by name")
        if len(names) != len(set(names)):
            raise ValueError("runtime distribution names must be unique")
        return distributions

    @field_validator("identities")
    @classmethod
    def validate_identities(
        cls, identities: tuple[RuntimeIdentity, ...]
    ) -> tuple[RuntimeIdentity, ...]:
        if len(identities) > MAX_RECORDED_DISTRIBUTIONS:
            raise ValueError(
                f"at most {MAX_RECORDED_DISTRIBUTIONS} runtime identities may be reported"
            )
        names = [identity.name for identity in identities]
        if names != sorted(names):
            raise ValueError("runtime identities must be sorted by name")
        if len(names) != len(set(names)):
            raise ValueError("runtime identity names must be unique")
        return identities


def normalize_distribution_name(name: str) -> str:
    """Validate and normalize one explicit distribution name.

    Distribution names, unlike import-package names, are the identifiers
    accepted by :mod:`importlib.metadata`.  The normalization matches the
    separator and case rules used by Python package indexes.
    """

    if not isinstance(name, str):
        raise TypeError("distribution names must be strings")
    if not _DISTRIBUTION_NAME.fullmatch(name):
        raise ValueError(
            "distribution names must contain only ASCII letters, digits, '.', '_' or '-' "
            "and start and end with a letter or digit"
        )
    return _DISTRIBUTION_SEPARATOR.sub("-", name).lower()


def normalize_distribution_names(names: Iterable[str]) -> tuple[str, ...]:
    """Return sorted, unique normalized names for an explicit recording list."""

    if isinstance(names, (str, bytes)):
        raise TypeError("recorded distributions must be an iterable of names, not a string")
    normalized: set[str] = set()
    for name in names:
        canonical = normalize_distribution_name(name)
        if canonical in normalized:
            raise ValueError(f"duplicate distribution name after normalization: {canonical}")
        normalized.add(canonical)
        if len(normalized) > MAX_RECORDED_DISTRIBUTIONS:
            raise ValueError(
                f"at most {MAX_RECORDED_DISTRIBUTIONS} explicit distributions may be recorded"
            )
    return tuple(sorted(normalized))


def normalize_distribution_requirements(
    requirements: Mapping[str, str],
) -> dict[str, str]:
    """Validate and canonicalize an explicit name-to-PEP-440 requirement map."""

    if not isinstance(requirements, Mapping):
        raise TypeError("required distributions must be a mapping of names to specifiers")
    normalized: dict[str, str] = {}
    for raw_name, raw_specifier in requirements.items():
        name = normalize_distribution_name(raw_name)
        if name in normalized:
            raise ValueError(f"duplicate distribution requirement after normalization: {name}")
        if not isinstance(raw_specifier, str):
            raise TypeError("distribution specifiers must be strings")
        if len(raw_specifier) > MAX_DISTRIBUTION_SPECIFIER_LENGTH:
            raise ValueError(
                "distribution specifiers must contain at most "
                f"{MAX_DISTRIBUTION_SPECIFIER_LENGTH} characters"
            )
        if any(ord(character) < 32 or ord(character) > 126 for character in raw_specifier):
            raise ValueError("distribution specifiers must contain only printable ASCII")
        try:
            specifier = SpecifierSet(raw_specifier)
        except InvalidSpecifier as error:
            raise ValueError(f"invalid PEP 440 specifier for {name}") from error
        if any(item.operator == "===" for item in specifier):
            raise ValueError("PEP 440 arbitrary equality specifiers are not supported")
        normalized[name] = str(specifier)
        if len(normalized) > MAX_RECORDED_DISTRIBUTIONS:
            raise ValueError(f"at most {MAX_RECORDED_DISTRIBUTIONS} distributions may be required")
    return dict(sorted(normalized.items()))


def distribution_satisfies_requirement(version: str | None, specifier: str) -> bool:
    """Whether one bounded metadata version satisfies a validated PEP 440 specifier."""

    if version is None:
        return False
    try:
        parsed_version = Version(version)
        parsed_specifier = SpecifierSet(specifier)
    except (InvalidVersion, InvalidSpecifier):
        return False
    return parsed_version in parsed_specifier


def runtime_contract_failures(
    runtime: RuntimeProvenance,
    *,
    expected_parity_version: str | None,
    required_distributions: Mapping[str, str],
) -> tuple[str, ...]:
    """Return bounded, value-free paths for unmet worker runtime requirements."""

    failures: list[str] = []
    if expected_parity_version is not None and runtime.parity_version != expected_parity_version:
        failures.append("parity_version")
    observed = {distribution.name: distribution for distribution in runtime.distributions}
    for name, specifier in normalize_distribution_requirements(required_distributions).items():
        distribution = observed.get(name)
        if distribution is None:
            failures.append(f"distributions.{name}.unavailable")
        elif distribution.status != "installed":
            failures.append(f"distributions.{name}.{distribution.status}")
        elif not distribution_satisfies_requirement(distribution.version, specifier):
            failures.append(f"distributions.{name}.version")
    return tuple(failures)


def _safe_runtime_label(value: str) -> str:
    return value if _SAFE_RUNTIME_LABEL.fullmatch(value) else "unknown"


def _distribution_provenance(name: str) -> DistributionProvenance:
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return DistributionProvenance(name=name, status="missing")
    except Exception:
        # Metadata backends can be extended by the environment.  Their raw
        # exceptions may contain paths or other local details, so only the
        # bounded status crosses the provenance boundary.
        return DistributionProvenance(name=name, status="unavailable")
    if not isinstance(version, str) or not _SAFE_VERSION.fullmatch(version):
        return DistributionProvenance(name=name, status="unavailable")
    return DistributionProvenance(name=name, status="installed", version=version)


@lru_cache(maxsize=128)
def _collect_runtime_provenance_cached(
    explicit_distributions: tuple[str, ...],
) -> RuntimeProvenance:
    names = tuple(sorted(set(CORE_DISTRIBUTIONS).union(explicit_distributions)))
    return RuntimeProvenance(
        executor="parity-python",
        runtime_name="python",
        runtime_version=_safe_runtime_label(platform.python_version()),
        python_implementation=_safe_runtime_label(platform.python_implementation()),
        python_version=_safe_runtime_label(platform.python_version()),
        platform_system=_safe_runtime_label(platform.system()),
        platform_machine=_safe_runtime_label(platform.machine()),
        parity_version=_safe_runtime_label(__version__),
        distributions=tuple(_distribution_provenance(name) for name in names),
        identities=(),
    )


def collect_runtime_provenance(
    record_distributions: Iterable[str] = (),
) -> RuntimeProvenance:
    """Collect data-safe provenance for this interpreter.

    Core dataframe dependencies are always included.  Additional target
    distributions must be named explicitly, avoiding a broad and potentially
    sensitive inventory of everything installed in the environment.
    """

    explicit = normalize_distribution_names(record_distributions)
    return _collect_runtime_provenance_cached(explicit)


def diff_runtime(expected: RuntimeProvenance, actual: RuntimeProvenance) -> tuple[str, ...]:
    """Return stable, value-free paths for runtime-provenance differences."""

    differences: list[str] = []
    for field in (
        "executor",
        "runtime_name",
        "runtime_version",
        "python_implementation",
        "python_version",
        "platform_system",
        "platform_machine",
        "parity_version",
    ):
        if getattr(expected, field) != getattr(actual, field):
            differences.append(field)
    expected_distributions = {item.name: item for item in expected.distributions}
    actual_distributions = {item.name: item for item in actual.distributions}
    for name in sorted(set(expected_distributions).union(actual_distributions)):
        if expected_distributions.get(name) != actual_distributions.get(name):
            differences.append(f"distributions.{name}")
    expected_identities = {item.name: item for item in expected.identities}
    actual_identities = {item.name: item for item in actual.identities}
    for name in sorted(set(expected_identities).union(actual_identities)):
        if expected_identities.get(name) != actual_identities.get(name):
            differences.append(f"identities.{name}")
    return tuple(differences)


def _canonical_path(
    path: Path,
    base_directory: Path | None,
    *,
    dereference: bool = True,
) -> dict[str, str]:
    if not path.is_absolute():
        return {"$path": path.as_posix()}
    if base_directory is not None:
        try:
            normalized = path.resolve() if dereference else Path(os.path.abspath(path))
            normalized_base = (
                base_directory.resolve() if dereference else Path(os.path.abspath(base_directory))
            )
            relative = normalized.relative_to(normalized_base)
        except (OSError, ValueError):
            pass
        else:
            return {"$path": relative.as_posix()}
    # External absolute locations are execution details and can contain user
    # names or workspace identifiers.  Their values are intentionally neither
    # retained nor hashed.
    return {"$path": "<external>"}


def _canonical_python_path(
    python: Path,
    *,
    base_directory: Path | None,
    workdir: object,
) -> dict[str, str]:
    """Normalize a launch path against config base, then its callable workdir."""

    bases = [base_directory]
    if isinstance(workdir, Path):
        bases.append(workdir)
    for base in bases:
        if base is None:
            continue
        canonical = _canonical_path(python, base, dereference=False)
        if canonical["$path"] != "<external>":
            return canonical
    return {"$path": "<external>"}


def _canonical_value(
    value: Any,
    *,
    key: str | None = None,
    base_directory: Path | None = None,
    path: tuple[str, ...] = (),
) -> Any:
    if key == "artifact_dir":
        return _OMIT
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", by_alias=True)
    if key == "environment" and isinstance(value, Mapping):
        return {str(item): "<redacted>" for item in sorted(value, key=str)}
    if key and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Path):
        return _canonical_path(value, base_directory, dereference=key != "python")
    if isinstance(value, Mapping):
        # Input declaration order is part of a positional bundle's callable
        # contract. Preserve only the actual case-level input-bundle mapping;
        # an ordinary static kwarg may also legitimately be named ``inputs``
        # and remains a normal order-insensitive JSON mapping.
        if path == ("cases", "input_bundle", "inputs"):
            return [
                {
                    "name": str(item_key),
                    "spec": _canonical_value(
                        item_value,
                        key=str(item_key),
                        base_directory=base_directory,
                        path=(*path, str(item_key)),
                    ),
                }
                for item_key, item_value in value.items()
            ]
        canonical: dict[str, Any] = {}
        for item_key in sorted(value, key=str):
            item_name = str(item_key)
            item_value = value[item_key]
            if item_name == "python" and isinstance(item_value, Path):
                item = _canonical_python_path(
                    item_value,
                    base_directory=base_directory,
                    workdir=value.get("workdir"),
                )
            else:
                item = _canonical_value(
                    item_value,
                    key=item_name,
                    base_directory=base_directory,
                    path=(*path, item_name),
                )
            if item is not _OMIT:
                canonical[item_name] = item
        return canonical
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item, base_directory=base_directory, path=path) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item, base_directory=base_directory, path=path) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return {"$float": "nan"}
        return {"$float": "infinity" if value > 0 else "-infinity"}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # Configuration is normally constrained to Pydantic/JSON values.  A type
    # marker keeps programmatic extensions deterministic without stringifying
    # an object whose repr could expose data or a path.
    return {"$type": f"{type(value).__module__}.{type(value).__qualname__}"}


def effective_config_sha256(
    config: BaseModel | Mapping[str, Any],
    *,
    selected_cases: set[str] | None = None,
    base_directory: str | Path | None = None,
) -> str:
    """Hash the effective, data-safe verification contract.

    ``artifact_dir`` is excluded because it does not affect observations.
    Environment and secret-like values are replaced before hashing.  When a
    base directory is supplied, project-local paths are normalized relative to
    it; external absolute paths collapse to a non-sensitive marker.
    """

    raw: Any
    if isinstance(config, BaseModel):
        raw = config.model_dump(mode="python", by_alias=True)
    else:
        raw = dict(config)
    if not isinstance(raw, dict):  # pragma: no cover - defensive type narrowing
        raise TypeError("config must serialize to a mapping")

    if selected_cases is not None:
        cases = raw.get("cases")
        if not isinstance(cases, list):
            raise ValueError("selected cases require a top-level cases list")
        known = {
            case.get("name")
            for case in cases
            if isinstance(case, Mapping) and isinstance(case.get("name"), str)
        }
        unknown = selected_cases - known
        if unknown:
            raise ValueError(f"unknown selected case(s): {', '.join(sorted(unknown))}")
        raw["cases"] = [
            case
            for case in cases
            if isinstance(case, Mapping) and case.get("name") in selected_cases
        ]

    base = Path(base_directory) if base_directory is not None else _common_config_base(raw)
    canonical = _canonical_value(raw, base_directory=base)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _common_config_base(raw: Mapping[str, Any]) -> Path | None:
    """Infer a shared configured workdir without retaining any absolute path."""

    cases = raw.get("cases")
    if not isinstance(cases, list):
        return None
    workdirs: set[Path] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            continue
        for side in ("reference", "candidate"):
            spec = case.get(side)
            if not isinstance(spec, Mapping):
                continue
            workdir = spec.get("workdir")
            if not isinstance(workdir, Path) or not workdir.is_absolute():
                continue
            workdirs.add(workdir.resolve())
    if len(workdirs) == 1:
        return next(iter(workdirs))
    return None


__all__ = [
    "CORE_DISTRIBUTIONS",
    "MAX_DISTRIBUTION_SPECIFIER_LENGTH",
    "MAX_RECORDED_DISTRIBUTIONS",
    "DistributionProvenance",
    "RuntimeProvenance",
    "collect_runtime_provenance",
    "diff_runtime",
    "distribution_satisfies_requirement",
    "effective_config_sha256",
    "normalize_distribution_name",
    "normalize_distribution_names",
    "normalize_distribution_requirements",
    "runtime_contract_failures",
]
