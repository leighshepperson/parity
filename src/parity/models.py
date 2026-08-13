"""Stable configuration and result contracts used throughout Parity."""

from __future__ import annotations

import json
import keyword
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic import JsonValue as JsonValue

from parity._version import __version__
from parity.provenance import (
    MAX_RECORDED_DISTRIBUTIONS,
    RuntimeProvenance,
    normalize_distribution_names,
)
from parity.targets import IMPORT_TARGET_PATTERN

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
        if self.categories is not None:
            category_markers = [
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in self.categories
            ]
            if len(category_markers) != len(set(category_markers)):
                raise ValueError("categories must contain unique values")
            if not self.nullable and any(value is None for value in self.categories):
                raise ValueError("non-nullable columns cannot include null in categories")
        if self.minimum is not None and self.maximum is not None:
            try:
                if self.minimum > self.maximum:  # type: ignore[operator]
                    raise ValueError("minimum cannot be greater than maximum")
            except TypeError:
                pass
        return self


class SortedBy(StrictModel):
    """Require rows to be lexicographically ordered by selected columns."""

    kind: Literal["sorted_by"] = "sorted_by"
    columns: list[str] = Field(min_length=1)
    descending: bool = False
    nulls: Literal["first", "last"] = "last"

    @field_validator("columns")
    @classmethod
    def unique_columns(cls, columns: list[str]) -> list[str]:
        if len(columns) != len(set(columns)):
            raise ValueError("sorted_by columns must be unique")
        if any(not column for column in columns):
            raise ValueError("sorted_by column names cannot be empty")
        return columns


class RowComparison(StrictModel):
    """Require a positional comparison between two columns on every non-null row."""

    kind: Literal["row_comparison"] = "row_comparison"
    left: str = Field(min_length=1)
    operator: Literal["lt", "le", "eq", "ge", "gt"]
    right: str = Field(min_length=1)


FrameConstraint = Annotated[
    SortedBy | RowComparison,
    Field(discriminator="kind"),
]


class FrameSchema(StrictModel):
    """Schema and domain constraints for generated tabular inputs."""

    columns: list[ColumnSchema] = Field(min_length=1)
    min_rows: int = Field(default=0, ge=0)
    max_rows: int = Field(default=30, ge=0, le=10_000)
    unique_together: list[list[str]] = Field(default_factory=list)
    constraints: list[FrameConstraint] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_schema(self) -> FrameSchema:
        names = [column.name for column in self.columns]
        columns = {column.name: column for column in self.columns}
        if len(set(names)) != len(names):
            raise ValueError("column names must be unique")
        if self.min_rows > self.max_rows:
            raise ValueError("min_rows cannot exceed max_rows")
        if any(not group for group in self.unique_together):
            raise ValueError("unique_together groups cannot be empty")
        if any(len(group) != len(set(group)) for group in self.unique_together):
            raise ValueError("columns within a unique_together group must be unique")
        normalized_groups = [tuple(group) for group in self.unique_together]
        if len(normalized_groups) != len(set(normalized_groups)):
            raise ValueError("unique_together groups must be unique")
        unknown = {
            name for group in self.unique_together for name in group if name not in set(names)
        }
        if unknown:
            raise ValueError(f"unique_together references unknown columns: {sorted(unknown)}")

        from parity.canonical import dtype_family

        sortable_families = {
            "boolean",
            "integer",
            "float",
            "decimal",
            "string",
            "category",
            "date",
            "datetime",
            "time",
            "duration",
        }
        seen_constraints: set[tuple[object, ...]] = set()
        sorted_constraints = 0
        for constraint in self.constraints:
            if isinstance(constraint, SortedBy):
                marker: tuple[object, ...] = (
                    constraint.kind,
                    *constraint.columns,
                    constraint.descending,
                    constraint.nulls,
                )
            elif constraint.operator == "eq":
                marker = (
                    constraint.kind,
                    constraint.operator,
                    *sorted((constraint.left, constraint.right)),
                )
            elif constraint.operator in {"ge", "gt"}:
                normalized = "le" if constraint.operator == "ge" else "lt"
                marker = (constraint.kind, normalized, constraint.right, constraint.left)
            else:
                marker = (
                    constraint.kind,
                    constraint.operator,
                    constraint.left,
                    constraint.right,
                )
            if marker in seen_constraints:
                raise ValueError("frame constraints must be unique")
            seen_constraints.add(marker)
            if isinstance(constraint, SortedBy):
                sorted_constraints += 1
                unknown = set(constraint.columns) - set(names)
                if unknown:
                    raise ValueError(f"sorted_by references unknown columns: {sorted(unknown)}")
                unsupported = [
                    name
                    for name in constraint.columns
                    if dtype_family(columns[name].dtype) not in sortable_families
                ]
                if unsupported:
                    raise ValueError(
                        "sorted_by requires scalar orderable columns; unsupported columns: "
                        f"{unsupported}"
                    )
            else:
                unknown = {constraint.left, constraint.right} - set(names)
                if unknown:
                    raise ValueError(
                        f"row_comparison references unknown columns: {sorted(unknown)}"
                    )
                if constraint.left == constraint.right and constraint.operator in {"lt", "gt"}:
                    raise ValueError("a strict row_comparison cannot compare a column with itself")
                left = columns[constraint.left]
                right = columns[constraint.right]
                left_family = dtype_family(left.dtype)
                right_family = dtype_family(right.dtype)
                numeric = {"integer", "float", "decimal"}
                textual = {"string", "category"}
                comparable = (
                    left_family == right_family
                    or {left_family, right_family} <= numeric
                    or {left_family, right_family} <= textual
                )
                if not comparable or left_family not in sortable_families:
                    raise ValueError(
                        "row_comparison columns must have comparable scalar dtype families: "
                        f"{constraint.left} is {left_family}, "
                        f"{constraint.right} is {right_family}"
                    )
                if left_family == "datetime" and left.timezone != right.timezone:
                    raise ValueError("row_comparison datetime columns must have the same timezone")
        if sorted_constraints > 1:
            raise ValueError("a frame schema can contain at most one sorted_by constraint")
        return self


class InputSpec(StrictModel):
    """Fixture and/or generation schema for one named input frame."""

    fixture: Path | None = None
    input_schema: FrameSchema | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )

    model_config = ConfigDict(extra="forbid", validate_assignment=True, populate_by_name=True)

    @model_validator(mode="after")
    def require_input_contract(self) -> InputSpec:
        if self.fixture is None and self.input_schema is None:
            raise ValueError("an input requires either fixture or schema")
        return self


class KeyRef(StrictModel):
    """A possibly composite key in one named input."""

    input: str = Field(min_length=1)
    columns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_key(self) -> KeyRef:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("key columns must be unique")
        if any(not column for column in self.columns):
            raise ValueError("key column names cannot be empty")
        return self


class KeyOverlap(StrictModel):
    """Require a minimum number of distinct non-null keys to be shared."""

    kind: Literal["key_overlap"] = "key_overlap"
    left: KeyRef
    right: KeyRef
    min_shared: int = Field(default=1, ge=1)


class ForeignKey(StrictModel):
    """Require each non-null child key to occur in the parent input."""

    kind: Literal["foreign_key"] = "foreign_key"
    child: KeyRef
    parent: KeyRef
    allow_nulls: bool = True


class EqualRowCount(StrictModel):
    """Require selected inputs to contain the same number of rows."""

    kind: Literal["equal_row_count"] = "equal_row_count"
    inputs: list[str] = Field(min_length=2, max_length=3)

    @field_validator("inputs")
    @classmethod
    def unique_inputs(cls, inputs: list[str]) -> list[str]:
        if len(inputs) != len(set(inputs)):
            raise ValueError("equal_row_count inputs must be unique")
        return inputs


CardinalityName = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class Cardinality(StrictModel):
    """Declare which side(s) of a relationship must have unique keys."""

    kind: Literal["cardinality"] = "cardinality"
    left: KeyRef
    right: KeyRef
    relationship: CardinalityName


Relationship = Annotated[
    KeyOverlap | ForeignKey | EqualRowCount | Cardinality,
    Field(discriminator="kind"),
]


class InputBundle(StrictModel):
    """Two or three named frames and their relational generation constraints."""

    inputs: dict[str, InputSpec] = Field(min_length=2, max_length=3)
    relationships: list[Relationship] = Field(default_factory=list)
    binding: Literal["keyword", "positional"] = "keyword"

    @model_validator(mode="after")
    def validate_bundle(self) -> InputBundle:
        for name in self.inputs:
            if not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(f"input name {name!r} must be a non-keyword Python identifier")

        fixture_count = sum(spec.fixture is not None for spec in self.inputs.values())
        if fixture_count not in {0, len(self.inputs)}:
            raise ValueError("input_bundle fixtures must be provided for every input or none")

        seen: set[str] = set()
        for relationship in self.relationships:
            marker = json.dumps(relationship.model_dump(mode="json"), sort_keys=True)
            if marker in seen:
                raise ValueError("input relationships must be unique")
            seen.add(marker)

            refs: list[KeyRef] = []
            if isinstance(relationship, KeyOverlap | Cardinality):
                refs = [relationship.left, relationship.right]
            elif isinstance(relationship, ForeignKey):
                refs = [relationship.child, relationship.parent]
            elif isinstance(relationship, EqualRowCount):
                unknown = set(relationship.inputs) - self.inputs.keys()
                if unknown:
                    raise ValueError(
                        f"equal_row_count references unknown inputs: {sorted(unknown)}"
                    )

            for ref in refs:
                if ref.input not in self.inputs:
                    raise ValueError(f"key references unknown input {ref.input!r}")
                schema = self.inputs[ref.input].input_schema
                if schema is not None:
                    unknown_columns = set(ref.columns) - {column.name for column in schema.columns}
                    if unknown_columns:
                        raise ValueError(
                            f"key for input {ref.input!r} references unknown columns: "
                            f"{sorted(unknown_columns)}"
                        )

            if refs and len(refs[0].columns) != len(refs[1].columns):
                raise ValueError("paired relationship keys must contain the same number of columns")

        return self


class CallableSpec(StrictModel):
    """Importable implementation and the environment in which it should run."""

    target: str = Field(pattern=IMPORT_TARGET_PATTERN)
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
    row_order: Literal["strict", "ignore", "keyed"] = "strict"
    row_keys: list[str] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def validate_row_alignment(self) -> ComparisonPolicy:
        if any(not name for name in self.row_keys):
            raise ValueError("row_keys cannot contain empty column names")
        normalized_keys = [
            name if self.names == "strict" else name.casefold() for name in self.row_keys
        ]
        if len(normalized_keys) != len(set(normalized_keys)):
            raise ValueError("row_keys must contain unique column names")
        if self.row_order == "keyed" and not self.row_keys:
            raise ValueError("row_order='keyed' requires at least one row key")
        if self.row_order != "keyed" and self.row_keys:
            raise ValueError("row_keys can only be used with row_order='keyed'")

        normalized_ignored = {
            name if self.names == "strict" else name.casefold() for name in self.ignored_columns
        }
        overlap = sorted(set(normalized_keys) & normalized_ignored)
        if overlap:
            raise ValueError("row_keys cannot also be ignored columns")
        if (
            self.row_order == "keyed"
            and self.null_nan_equal
            and not (self.null_equal and self.nan_equal)
        ):
            raise ValueError(
                "keyed null_nan_equal requires null_equal and nan_equal so keys are reflexive"
            )
        return self


class GenerationConfig(StrictModel):
    """Deterministic and property-based exploration limits."""

    max_examples: int = Field(default=100, ge=1, le=100_000)
    max_findings: int = Field(default=1, ge=1, le=20)
    stability_repeats: int = Field(default=2, ge=1, le=10)
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
    input_bundle: InputBundle | None = None
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
        has_single_input = self.fixture is not None or self.input_schema is not None
        if not has_single_input and self.input_bundle is None:
            raise ValueError("a case requires either fixture or schema, or input_bundle")
        if has_single_input and self.input_bundle is not None:
            raise ValueError("a case cannot combine fixture or schema with input_bundle")
        if self.input_bundle is not None and self.input_bundle.binding == "keyword":
            if self.static_args:
                raise ValueError("keyword input_bundle binding cannot be combined with static_args")
            collisions = self.input_bundle.inputs.keys() & self.static_kwargs.keys()
            if collisions:
                raise ValueError(f"input names collide with static_kwargs: {sorted(collisions)}")
        return self


class ParityConfig(StrictModel):
    """Top-level parity.toml document."""

    version: Literal[1] = 1
    artifact_dir: Path = Path(".parity")
    cases: list[CaseConfig] = Field(min_length=1)
    fail_fast: bool = False
    _base_directory: Path | None = PrivateAttr(default=None)

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
    finding_signature: str | None = Field(default=None, pattern=r"^ms1:[0-9a-f]{64}$")


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

    @model_validator(mode="before")
    @classmethod
    def discard_serialized_finding_count(cls, value: object) -> object:
        """Accept model dumps while keeping the finding count truly derived."""

        if isinstance(value, dict) and "findings_discovered" in value:
            return {key: item for key, item in value.items() if key != "findings_discovered"}
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def findings_discovered(self) -> int:
        """Number of distinct signed semantic findings in current evidence."""

        return len(
            {
                failure.finding_signature
                for failure in self.failures
                if failure.finding_signature is not None
            }
        )


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
