"""Stable configuration and result contracts used throughout Parity."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import JsonValue as JsonValue

from parity._version import __version__
from parity.provenance import (
    MAX_RECORDED_DISTRIBUTIONS,
    RuntimeProvenance,
    normalize_distribution_names,
)

AdapterName = Literal["auto", "pandas", "polars", "arrow"]
PandasInput = Literal["arrow", "native"]


class StrictModel(BaseModel):
    """Base model that rejects misspelled configuration keys."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class MismatchKind(StrEnum):
    COLUMN = "column"
    DTYPE = "dtype"
    EXCEPTION = "exception"
    MUTATION = "mutation"
    PERFORMANCE = "performance"
    ROW = "row"
    SCHEMA = "schema"
    SHAPE = "shape"
    VALUE = "value"


class ColumnSchema(StrictModel):
    """Portable description of a generated dataframe column."""

    name: str = Field(min_length=1)
    dtype: str = Field(description="Portable dtype or concrete pandas/Polars dtype name")
    nullable: bool = True
    unique: bool = False
    minimum: JsonValue = None
    maximum: JsonValue = None
    categories: list[JsonValue] | None = None
    examples: list[JsonValue] = Field(default_factory=list)
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> ColumnSchema:
        if self.categories is not None and not self.categories:
            raise ValueError("categories must contain at least one value")
        if self.minimum is not None and self.maximum is not None:
            try:
                if self.minimum > self.maximum:  # type: ignore[operator]
                    raise ValueError("minimum cannot be greater than maximum")
            except TypeError:
                pass
        return self


class FrameSchema(StrictModel):
    """Schema and domain constraints for generated tabular inputs."""

    columns: list[ColumnSchema] = Field(min_length=1)
    min_rows: int = Field(default=0, ge=0)
    max_rows: int = Field(default=30, ge=0, le=10_000)
    unique_together: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_schema(self) -> FrameSchema:
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("column names must be unique")
        if self.min_rows > self.max_rows:
            raise ValueError("min_rows cannot exceed max_rows")
        unknown = {
            name for group in self.unique_together for name in group if name not in set(names)
        }
        if unknown:
            raise ValueError(f"unique_together references unknown columns: {sorted(unknown)}")
        return self


class CallableSpec(StrictModel):
    """Importable implementation and the environment in which it should run."""

    target: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
    adapter: AdapterName = "auto"
    pandas_input: PandasInput = "arrow"
    python: Path | None = None
    workdir: Path | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    record_distributions: list[str] = Field(
        default_factory=list, max_length=MAX_RECORDED_DISTRIBUTIONS
    )

    @field_validator("record_distributions")
    @classmethod
    def normalize_recorded_distributions(cls, names: list[str]) -> list[str]:
        try:
            return list(normalize_distribution_names(names))
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from error


class ComparisonPolicy(StrictModel):
    """Explicit definition of semantic equivalence."""

    column_order: Literal["strict", "ignore"] = "strict"
    row_order: Literal["strict", "ignore"] = "strict"
    dtype: Literal["strict", "compatible", "ignore"] = "compatible"
    names: Literal["strict", "case_insensitive"] = "strict"
    null_equal: bool = True
    nan_equal: bool = True
    null_nan_equal: bool = False
    signed_zero_equal: bool = True
    check_exceptions: bool = True
    check_input_mutation: bool = True
    rtol: float = Field(default=1e-7, ge=0)
    atol: float = Field(default=0.0, ge=0)
    datetime_tolerance_ns: int = Field(default=0, ge=0)
    ignored_columns: list[str] = Field(default_factory=list)


class GenerationConfig(StrictModel):
    """Deterministic and property-based exploration limits."""

    max_examples: int = Field(default=100, ge=1, le=100_000)
    seed: int | None = None
    deadline_ms: int | None = Field(default=None, ge=1)
    adversarial_examples: bool = True
    shrink: bool = True
    derandomize: bool = False
    suppress_too_slow: bool = True


class PerformanceConfig(StrictModel):
    """Benchmark policy applied after semantic verification."""

    enabled: bool = True
    warmups: int = Field(default=1, ge=0, le=100)
    repeats: int = Field(default=5, ge=1, le=1_000)
    max_slowdown: float | None = Field(default=1.25, ge=0)
    max_memory_ratio: float | None = Field(default=1.50, ge=0)
    min_reference_ms: float = Field(default=1.0, ge=0)
    fail_on_regression: bool = False


class CaseConfig(StrictModel):
    """One reference-versus-candidate verification campaign."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    reference: CallableSpec
    candidate: CallableSpec
    fixture: Path | None = None
    input_schema: FrameSchema | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )
    static_args: list[JsonValue] = Field(default_factory=list)
    static_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    comparison: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    timeout_seconds: float = Field(default=30.0, gt=0, le=3_600)
    tags: set[str] = Field(default_factory=set)

    model_config = ConfigDict(extra="forbid", validate_assignment=True, populate_by_name=True)

    @model_validator(mode="after")
    def require_input_contract(self) -> CaseConfig:
        if self.fixture is None and self.input_schema is None:
            raise ValueError("a case requires either fixture or schema")
        return self


class ParityConfig(StrictModel):
    """Top-level parity.toml document."""

    version: Literal[1] = 1
    artifact_dir: Path = Path(".parity")
    cases: list[CaseConfig] = Field(min_length=1)
    fail_fast: bool = False

    @field_validator("cases")
    @classmethod
    def unique_case_names(cls, cases: list[CaseConfig]) -> list[CaseConfig]:
        names = [case.name for case in cases]
        if len(names) != len(set(names)):
            raise ValueError("case names must be unique")
        return cases


class Mismatch(StrictModel):
    kind: MismatchKind
    message: str
    path: str | None = None
    reference: JsonValue = None
    candidate: JsonValue = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class Diagnosis(StrictModel):
    """Evidence-based explanation for a class of observed difference."""

    code: str = Field(pattern=r"^[a-z0-9_.-]+$")
    title: str
    explanation: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[str] = Field(default_factory=list)
    documentation_url: str | None = None


class RunMetrics(StrictModel):
    duration_seconds: float = Field(ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    iterations: int = Field(default=1, ge=1)


class PerformanceResult(StrictModel):
    reference: RunMetrics
    candidate: RunMetrics
    speed_ratio: float | None = None
    memory_ratio: float | None = None
    regression: bool = False
    reasons: list[str] = Field(default_factory=list)


class ExampleResult(StrictModel):
    source: str
    status: Status
    mismatches: list[Mismatch] = Field(default_factory=list)
    artifact: Path | None = None
    reference_metrics: RunMetrics | None = None
    candidate_metrics: RunMetrics | None = None


class CaseProvenance(StrictModel):
    """Runtime identities observed on the two sides of one campaign."""

    reference: RuntimeProvenance | None = None
    candidate: RuntimeProvenance | None = None
    verification: Literal["captured", "verified", "unverified", "drifted"] = "captured"


class CaseResult(StrictModel):
    name: str
    status: Status
    examples_run: int = Field(default=0, ge=0)
    deterministic_examples: int = Field(default=0, ge=0)
    generated_examples: int = Field(default=0, ge=0)
    failures: list[ExampleResult] = Field(default_factory=list)
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    performance: PerformanceResult | None = None
    provenance: CaseProvenance | None = None
    elapsed_seconds: float = Field(default=0, ge=0)


class SuiteProvenance(StrictModel):
    """Orchestrator runtime and data-safe effective-configuration fingerprint."""

    orchestrator: RuntimeProvenance
    config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SuiteResult(StrictModel):
    status: Status
    cases: list[CaseResult]
    elapsed_seconds: float = Field(default=0, ge=0)
    parity_version: str = Field(default_factory=lambda: __version__)
    provenance: SuiteProvenance | None = None

    @property
    def passed(self) -> bool:
        return self.status is Status.PASSED
