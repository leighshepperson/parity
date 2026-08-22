"""Stable configuration and result contracts used throughout Parity."""

from __future__ import annotations

import json
import keyword
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    normalize_distribution_requirements,
)
from parity.targets import is_import_target

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
    regex: str | None = Field(default=None, max_length=4_096)
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> ColumnSchema:
        from parity.canonical import dtype_family

        family = dtype_family(self.dtype)
        if any(
            value is not None for value in (self.regex, self.min_length, self.max_length)
        ) and family not in {"string", "category"}:
            raise ValueError("regex and length bounds are only valid for text columns")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot be greater than max_length")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        if self.timezone is not None:
            if family != "datetime":
                raise ValueError("timezone is only valid for datetime columns")
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown IANA timezone: {self.timezone!r}") from exc
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
    """One Python or protocol-command implementation and its environment."""

    target: str | None = None
    command: list[str] | None = None
    canonicalizer: str | None = None
    adapter: AdapterName = "auto"
    pandas_input: PandasInput = "arrow"
    python: Path | None = None
    workdir: Path | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    record_distributions: list[str] = Field(
        default_factory=list, max_length=MAX_RECORDED_DISTRIBUTIONS
    )
    required_distributions: dict[str, str] = Field(
        default_factory=dict, max_length=MAX_RECORDED_DISTRIBUTIONS
    )
    native_threads: int | None = Field(default=None, ge=1, le=256)

    @field_validator("target", "canonicalizer")
    @classmethod
    def validate_target(cls, target: str | None) -> str | None:
        if target is not None and not is_import_target(target):
            raise ValueError("Python targets must use module.path:callable.path syntax")
        return target

    @field_validator("command")
    @classmethod
    def validate_command(cls, command: list[str] | None) -> list[str] | None:
        if command is None:
            return None
        if not 1 <= len(command) <= 64:
            raise ValueError("command must contain between 1 and 64 arguments")
        if any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > 4_096
            or "\x00" in argument
            or "\n" in argument
            or "\r" in argument
            for argument in command
        ):
            raise ValueError(
                "command arguments must be non-empty bounded strings without control lines"
            )
        return command

    @field_validator("record_distributions")
    @classmethod
    def normalize_recorded_distributions(cls, names: list[str]) -> list[str]:
        try:
            return list(normalize_distribution_names(names))
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from error

    @field_validator("required_distributions", mode="before")
    @classmethod
    def normalize_required_distributions(cls, requirements: object) -> dict[str, str]:
        try:
            return normalize_distribution_requirements(requirements)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def bound_explicit_distributions(self) -> CallableSpec:
        if (self.target is None) == (self.command is None):
            raise ValueError("exactly one of target or command is required")
        if self.command is not None:
            if self.python is not None:
                raise ValueError("command endpoints include their runtime and cannot set python")
            if self.canonicalizer is not None:
                raise ValueError(
                    "command endpoints canonicalize through the target protocol, not canonicalizer"
                )
            if self.adapter != "auto" or self.pandas_input != "arrow":
                raise ValueError("adapter and pandas_input apply only to Python target endpoints")
        explicit = set(self.record_distributions).union(self.required_distributions)
        if len(explicit) > MAX_RECORDED_DISTRIBUTIONS:
            raise ValueError(
                f"at most {MAX_RECORDED_DISTRIBUTIONS} explicit distributions may be used"
            )
        return self

    @property
    def provenance_distributions(self) -> tuple[str, ...]:
        """All explicitly observed names, including fail-closed requirements."""

        return tuple(sorted(set(self.record_distributions).union(self.required_distributions)))


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

    generator: str | None = None
    max_examples: int = Field(default=100, ge=1, le=100_000)
    max_findings: int = Field(default=10, ge=1, le=20)
    stability_repeats: int = Field(default=2, ge=1, le=10)
    search: bool = True
    seed: int | None = None
    deadline_ms: int | None = Field(default=None, ge=1)
    adversarial_examples: bool = True
    shrink: bool = True
    derandomize: bool = False
    suppress_too_slow: bool = True

    @field_validator("generator")
    @classmethod
    def validate_generator(cls, generator: str | None) -> str | None:
        if generator is not None and not is_import_target(generator):
            raise ValueError("generator must contain dotted Python identifiers as module:callable")
        return generator


class CompatibilityDecision(StrEnum):
    """Review decision for one observed behavioural difference class."""

    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"


class CompatibilityFinding(StrictModel):
    """One case-scoped finding decision in a reviewable compatibility budget."""

    case: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    finding_signature: str = Field(pattern=r"^ms3:[0-9a-f]{64}$")
    decision: CompatibilityDecision = CompatibilityDecision.REVIEW
    reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def require_review_rationale(self) -> CompatibilityFinding:
        if self.decision is not CompatibilityDecision.REVIEW and self.reason is None:
            raise ValueError("approved or rejected findings require a non-blank reason")
        return self


class CompatibilityBudget(StrictModel):
    """Versioned decisions defining which known differences may remain."""

    version: Literal[1] = 1
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: list[CompatibilityFinding] = Field(min_length=1, max_length=1_000)

    @field_validator("findings")
    @classmethod
    def unique_case_findings(
        cls, findings: list[CompatibilityFinding]
    ) -> list[CompatibilityFinding]:
        identities = [(finding.case, finding.finding_signature) for finding in findings]
        if len(identities) != len(set(identities)):
            raise ValueError("compatibility budget findings must be unique per case")
        return findings

    def approved_for(self, case: str) -> dict[str, CompatibilityFinding]:
        """Return exact reviewed approvals for one case, keyed by signature."""

        return {
            finding.finding_signature: finding
            for finding in self.findings
            if finding.case == case and finding.decision is CompatibilityDecision.APPROVED
        }


class PerformanceConfig(StrictModel):
    """Benchmark policy applied after semantic verification."""

    enabled: bool = True
    warmups: int = Field(default=1, ge=0, le=100)
    repeats: int = Field(default=9, ge=1, le=1_000)
    max_slowdown: float | None = Field(default=1.25, ge=0)
    max_memory_ratio: float | None = Field(default=1.50, ge=0)
    min_reference_ms: float = Field(default=1.0, ge=0)
    fail_on_regression: bool = False
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1)
    bootstrap_samples: int = Field(default=2_000, ge=100, le=100_000)

    @model_validator(mode="after")
    def require_evidence_for_enforced_regression(self) -> PerformanceConfig:
        if self.enabled and self.fail_on_regression and self.repeats < 5:
            raise ValueError("an enforced performance gate requires at least 5 repeats")
        return self


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
    reference_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    candidate_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    comparison: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    timeout_seconds: float = Field(default=30.0, gt=0, le=3_600)
    tags: set[str] = Field(default_factory=set)

    model_config = ConfigDict(extra="forbid", validate_assignment=True, populate_by_name=True)
    _base_directory: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def require_input_contract(self) -> CaseConfig:
        has_single_input = self.fixture is not None or self.input_schema is not None
        has_custom_generator = self.generation.generator is not None
        if not has_single_input and self.input_bundle is None and not has_custom_generator:
            raise ValueError(
                "a case requires either fixture or schema, input_bundle, or generation.generator"
            )
        if has_single_input and self.input_bundle is not None:
            raise ValueError("a case cannot combine fixture or schema with input_bundle")
        if has_custom_generator and (has_single_input or self.input_bundle is not None):
            raise ValueError(
                "generation.generator is a complete input contract and cannot be combined "
                "with fixture, schema, or input_bundle"
            )
        if has_custom_generator and not self.generation.search:
            raise ValueError("generation.generator requires generation.search=true")
        for side, endpoint_kwargs in (
            ("reference", self.reference_kwargs),
            ("candidate", self.candidate_kwargs),
        ):
            overlap = self.static_kwargs.keys() & endpoint_kwargs.keys()
            if overlap:
                raise ValueError(f"{side}_kwargs overlap static_kwargs: {sorted(overlap)}")
        if self.input_bundle is not None and self.input_bundle.binding == "keyword":
            if self.static_args:
                raise ValueError("keyword input_bundle binding cannot be combined with static_args")
            invocation_kwargs = (
                self.static_kwargs.keys()
                | self.reference_kwargs.keys()
                | self.candidate_kwargs.keys()
            )
            collisions = self.input_bundle.inputs.keys() & invocation_kwargs
            if collisions:
                raise ValueError(
                    f"input names collide with invocation kwargs: {sorted(collisions)}"
                )
        return self


class ParityConfig(StrictModel):
    """Top-level parity.toml document."""

    version: Literal[1] = 1
    artifact_dir: Path = Path(".parity")
    cases: list[CaseConfig] = Field(min_length=1)
    fail_fast: bool = False
    jobs: int = Field(default=1, ge=1, le=256)
    native_threads: int | None = Field(default=None, ge=1, le=256)
    compatibility_budget: CompatibilityBudget | None = None
    _base_directory: Path | None = PrivateAttr(default=None)

    @field_validator("cases")
    @classmethod
    def unique_case_names(cls, cases: list[CaseConfig]) -> list[CaseConfig]:
        names = [case.name for case in cases]
        if len(names) != len(set(names)):
            raise ValueError("case names must be unique")
        return cases

    @model_validator(mode="after")
    def validate_parallel_fail_fast(self) -> ParityConfig:
        if self.fail_fast and self.jobs > 1:
            raise ValueError("fail_fast=true cannot be combined with jobs greater than 1")
        if self.jobs > 1 and any(
            case.performance.enabled and case.performance.fail_on_regression for case in self.cases
        ):
            raise ValueError(
                "enforced performance gates require jobs=1 to avoid concurrent benchmark "
                "contention; use jobs=1 or set performance.fail_on_regression=false"
            )
        if self.compatibility_budget is not None:
            known = {case.name for case in self.cases}
            budget_cases = {finding.case for finding in self.compatibility_budget.findings}
            if unknown := budget_cases - known:
                raise ValueError(
                    "compatibility budget references unknown case(s): " + ", ".join(sorted(unknown))
                )
            for case in self.cases:
                approvals = self.compatibility_budget.approved_for(case.name)
                if len(approvals) >= case.generation.max_findings:
                    raise ValueError(
                        f"case {case.name!r} generation.max_findings must exceed its "
                        "approved compatibility findings so new differences remain discoverable"
                    )
        return self


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
    speed_ratio_ci: tuple[float, float] | None = None
    memory_ratio: float | None = None
    memory_ratio_ci: tuple[float, float] | None = None
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1)
    regression: bool = False
    reasons: list[str] = Field(default_factory=list)


class ExampleResult(StrictModel):
    source: str
    status: Status
    mismatches: list[Mismatch] = Field(default_factory=list)
    artifact: Path | None = None
    reference_metrics: RunMetrics | None = None
    candidate_metrics: RunMetrics | None = None
    finding_signature: str | None = Field(default=None, pattern=r"^ms3:[0-9a-f]{64}$")
    approved: bool = False

    @model_validator(mode="after")
    def validate_approval(self) -> ExampleResult:
        if self.approved and (self.status is not Status.FAILED or self.finding_signature is None):
            raise ValueError("only a signed semantic finding can be approved")
        return self


class CompatibilityResult(StrictModel):
    """Derived compatibility-budget outcome for one executed case."""

    approved_findings: list[str] = Field(default_factory=list)
    unapproved_findings: list[str] = Field(default_factory=list)
    unused_approvals: list[str] = Field(default_factory=list)

    @field_validator("approved_findings", "unapproved_findings", "unused_approvals")
    @classmethod
    def sorted_unique_signatures(cls, values: list[str]) -> list[str]:
        if any(re.fullmatch(r"ms3:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("compatibility results require finding signatures")
        if values != sorted(set(values)):
            raise ValueError("compatibility result signatures must be sorted and unique")
        return values

    @model_validator(mode="after")
    def disjoint_outcomes(self) -> CompatibilityResult:
        groups = (
            set(self.approved_findings),
            set(self.unapproved_findings),
            set(self.unused_approvals),
        )
        if any(groups[left] & groups[right] for left, right in ((0, 1), (0, 2), (1, 2))):
            raise ValueError("compatibility result signature groups must be disjoint")
        return self

    @property
    def within_budget(self) -> bool:
        return not self.unapproved_findings


class CaseProvenance(StrictModel):
    """Runtime identities observed on the two sides of one campaign."""

    reference: RuntimeProvenance | None = None
    candidate: RuntimeProvenance | None = None
    verification: Literal["captured", "verified", "drifted"] = "captured"


class CaseResult(StrictModel):
    name: str
    status: Status
    examples_run: int = Field(default=0, ge=0)
    deterministic_examples: int = Field(default=0, ge=0)
    generated_examples: int = Field(default=0, ge=0)
    finding_limit_reached: bool = False
    failures: list[ExampleResult] = Field(default_factory=list)
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    performance: PerformanceResult | None = None
    provenance: CaseProvenance | None = None
    compatibility: CompatibilityResult | None = None
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
