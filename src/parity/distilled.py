"""Distilled semantic contracts and reference-free candidate verification.

A distilled contract is deliberately narrower than a replay artifact.  It binds
the minimized input, the exact reference observation, the comparison policy,
and the candidate launch configuration.  It contains no reference endpoint, so
verification cannot execute legacy code by construction.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from parity._version import __version__
from parity.adapters import load_arrow_fixture
from parity.comparison import compare_observations, mismatch_signature
from parity.execution import (
    ArrowInputBundle,
    ExceptionInfo,
    ExecutionOutcome,
    Observation,
    execute_isolated,
)
from parity.models import (
    CallableSpec,
    CaseProvenance,
    CaseResult,
    ComparisonPolicy,
    ExampleResult,
    JsonValue,
    Mismatch,
    MismatchKind,
    RunMetrics,
    Status,
    StrictModel,
    SuiteProvenance,
    SuiteResult,
)
from parity.provenance import RuntimeProvenance, collect_runtime_provenance


class ContractError(ValueError):
    """Raised when a distilled contract cannot be created or trusted."""


def _safe_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("contract file paths must be canonical, relative, and contained")
    return value


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return "<redacted>" in value or "<path>" in value
    if isinstance(value, dict):
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redaction(item) for item in value)
    return False


class ContractPathBase(StrictModel):
    """Portable project-root binding for one contract directory."""

    kind: Literal["contract_ancestor"]
    levels: int = Field(ge=1, le=64)


class ContractFile(StrictModel):
    """Integrity metadata for one private contract data file."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class ContractInput(StrictModel):
    """One ordered Arrow input in a distilled example."""

    name: str = Field(min_length=1)
    file: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def valid_input_name(cls, value: str) -> str:
        if not value.isidentifier() or keyword.iskeyword(value):
            raise ValueError("contract input names must be valid Python identifiers")
        return value

    @field_validator("file")
    @classmethod
    def safe_input_file(cls, value: str) -> str:
        value = _safe_file(value)
        if not value.endswith(".arrow"):
            raise ValueError("contract inputs must be Arrow IPC files")
        return value


class ContractOutput(StrictModel):
    """Private serialized output belonging to one reference expectation."""

    kind: Literal["arrow", "json"]
    file: str = Field(min_length=1)

    @field_validator("file")
    @classmethod
    def safe_output_file(cls, value: str) -> str:
        return _safe_file(value)

    @model_validator(mode="after")
    def validate_extension(self) -> ContractOutput:
        expected = ".arrow" if self.kind == "arrow" else ".json"
        if not self.file.endswith(expected):
            raise ValueError(f"{self.kind} contract output must use {expected}")
        return self


class ContractException(StrictModel):
    """Portable exception semantics captured from the reference."""

    module: str = Field(min_length=1)
    type: str = Field(min_length=1)
    message: str
    message_fingerprint: str | None = Field(default=None, pattern=r"^ex1:[0-9a-f]{64}$")
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ContractExpectation(StrictModel):
    """Exact semantic observation captured from the retired reference."""

    outcome: Literal["returned", "raised"]
    metrics: RunMetrics
    has_table: bool
    has_value: bool
    output: ContractOutput | None = None
    exception: ContractException | None = None
    mutated_inputs: list[str] = Field(default_factory=list)
    return_type: str | None = None
    runtime: RuntimeProvenance

    @field_validator("mutated_inputs")
    @classmethod
    def unique_mutated_inputs(cls, values: list[str]) -> list[str]:
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("mutated input labels must be non-empty and unique")
        return values

    @model_validator(mode="after")
    def validate_outcome(self) -> ContractExpectation:
        if self.has_table and self.has_value:
            raise ValueError("one reference expectation cannot contain table and JSON outputs")
        if self.outcome == "raised":
            if (
                self.exception is None
                or self.output is not None
                or self.has_table
                or self.has_value
            ):
                raise ValueError("a raised reference expectation must contain only an exception")
            return self
        if self.exception is not None:
            raise ValueError("a returned reference expectation cannot contain an exception")
        if self.has_table != (self.output is not None and self.output.kind == "arrow"):
            raise ValueError("reference table metadata does not match its stored output")
        if self.has_value != (self.output is not None and self.output.kind == "json"):
            raise ValueError("reference value metadata does not match its stored output")
        return self


class ContractExample(StrictModel):
    """One minimized input and its immutable reference expectation."""

    finding_signature: str = Field(pattern=r"^ms3:[0-9a-f]{64}$")
    inputs: list[ContractInput] = Field(min_length=1, max_length=3)
    expected: ContractExpectation

    @model_validator(mode="after")
    def unique_inputs(self) -> ContractExample:
        names = [item.name for item in self.inputs]
        files = [item.file for item in self.inputs]
        if len(names) != len(set(names)) or len(files) != len(set(files)):
            raise ValueError("contract example inputs must have unique names and files")
        return self


class DistilledCase(StrictModel):
    """Candidate-only invocation contract for a set of minimized examples."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    candidate: CallableSpec
    comparison: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    static_args: list[JsonValue] = Field(default_factory=list)
    static_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    candidate_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0, le=3_600)
    input_binding: Literal["single", "keyword", "positional"]
    examples: list[ContractExample] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate_invocation(self) -> DistilledCase:
        overlap = self.static_kwargs.keys() & self.candidate_kwargs.keys()
        if overlap:
            raise ValueError(f"candidate_kwargs overlap static_kwargs: {sorted(overlap)}")
        invocation = (self.static_args, self.static_kwargs, self.candidate_kwargs)
        if any(_contains_redaction(value) for value in invocation):
            raise ValueError("redacted candidate arguments cannot form a distilled contract")
        candidate = self.candidate.model_dump(mode="json")
        if _contains_redaction(candidate.get("command")):
            raise ValueError("a redacted candidate command cannot form a distilled contract")
        for field in ("python", "workdir"):
            raw = candidate.get(field)
            if isinstance(raw, str) and Path(raw).is_absolute():
                raise ValueError("candidate paths in a distilled contract must be relative")
        signatures = [example.finding_signature for example in self.examples]
        if len(signatures) != len(set(signatures)):
            raise ValueError("finding signatures must be unique within a distilled case")
        input_names = [item.name for item in self.examples[0].inputs]
        if any([item.name for item in example.inputs] != input_names for example in self.examples):
            raise ValueError("all examples in a distilled case must use the same ordered inputs")
        if self.input_binding == "single" and input_names != ["input"]:
            raise ValueError("single-input contracts require exactly one input named 'input'")
        if self.input_binding != "single" and not 2 <= len(input_names) <= 3:
            raise ValueError("bundled contracts require two or three inputs")
        if self.input_binding == "keyword":
            collisions = set(input_names) & (
                self.static_kwargs.keys() | self.candidate_kwargs.keys()
            )
            if collisions:
                raise ValueError(f"input names collide with candidate kwargs: {sorted(collisions)}")
        return self


class DistilledContractManifest(StrictModel):
    """Versioned, candidate-only distilled contract stored as ``contract.json``."""

    version: Literal[1]
    created_at: datetime
    parity_version: str
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    path_base: ContractPathBase
    contains_input_data: Literal[True]
    contains_output_data: Literal[True]
    cases: list[DistilledCase] = Field(min_length=1)
    files: dict[str, ContractFile] = Field(min_length=1, max_length=1_024)

    @field_validator("files")
    @classmethod
    def safe_files(cls, files: dict[str, ContractFile]) -> dict[str, ContractFile]:
        for name in files:
            _safe_file(name)
            if name in {"contract.json", ".gitignore"}:
                raise ValueError("contract metadata cannot be listed as a private data file")
        return files

    @model_validator(mode="after")
    def bind_all_files(self) -> DistilledContractManifest:
        names = [case.name for case in self.cases]
        if len(names) != len(set(names)):
            raise ValueError("distilled case names must be unique")
        referenced: list[str] = []
        for case in self.cases:
            for example in case.examples:
                referenced.extend(item.file for item in example.inputs)
                if example.expected.output is not None:
                    referenced.append(example.expected.output.file)
        if len(referenced) != len(set(referenced)):
            raise ValueError("every private contract file must have one unique binding")
        if set(referenced) != set(self.files):
            raise ValueError("contract file manifest must exactly match its bound data files")
        return self


class DistillationResult(StrictModel):
    """Summary returned after atomically creating a distilled contract."""

    path: Path
    cases: int = Field(ge=1)
    examples: int = Field(ge=1)
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bound_json(root: Path, manifest: dict[str, Any], name: str) -> dict[str, Any]:
    metadata = manifest.get("files", {}).get(name)
    if not isinstance(metadata, dict):
        raise ContractError(f"artifact is missing required {name}; rerun parity check")
    path = root / name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"artifact is missing required {name}; rerun parity check") from exc
    if len(raw) != metadata.get("bytes") or hashlib.sha256(raw).hexdigest() != metadata.get(
        "sha256"
    ):
        raise ContractError(f"artifact integrity changed while reading {name}")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ContractError(f"artifact contains invalid {name}; rerun parity check") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"artifact contains invalid {name}; rerun parity check")
    return payload


def _source_file(
    root: Path,
    manifest: dict[str, Any],
    name: str,
    *,
    suffix: str,
) -> tuple[Path, dict[str, Any]]:
    if Path(name).name != name or not name.endswith(suffix):
        raise ContractError("artifact contains an unsafe contract data binding")
    metadata = manifest.get("files", {}).get(name)
    if not isinstance(metadata, dict):
        raise ContractError("artifact manifest does not bind required contract data")
    return root / name, metadata


def _source_artifact(
    *,
    case_name: str,
    artifact: Path,
    signature: str,
) -> dict[str, Any]:
    from parity.engine import (
        _REPLAY_BLOCKER_MESSAGES,
        _artifact_root,
        _replay_execution_root,
        _replay_inputs,
        _resolve_replay_paths,
        _verify_manifest,
    )
    from parity.evidence import _stored_finding

    root = _artifact_root(artifact)
    try:
        manifest = _verify_manifest(root)
        stored = _stored_finding(root)
    except Exception as exc:
        raise ContractError("report references an invalid finding artifact") from exc
    if manifest.get("case") != case_name:
        raise ContractError("report case does not match its finding artifact")
    if stored.finding_signature != signature:
        raise ContractError("report finding signature does not match its artifact")
    replay = _bound_json(root, manifest, "replay.json")
    if replay.get("version") != 2 or not isinstance(replay.get("case"), dict):
        raise ContractError("artifact has no supported replay contract; rerun parity check")
    blockers = replay.get("replay_blockers", {})
    if not isinstance(blockers, dict) or any(
        side not in {"artifact", "reference", "candidate"}
        or not isinstance(reason, str)
        or (side != "artifact" and reason not in _REPLAY_BLOCKER_MESSAGES)
        for side, reason in blockers.items()
    ):
        raise ContractError("artifact contains an invalid replay blocker declaration")
    if "artifact" in blockers or "candidate" in blockers:
        raise ContractError(
            "candidate is not reconstructable from this artifact; fix its project-relative "
            "configuration and rerun parity check"
        )
    try:
        project_root = _replay_execution_root(replay, root)
        replay_inputs = _replay_inputs(replay, manifest, root)
    except Exception as exc:
        raise ContractError(
            "artifact has no project-relative candidate replay; rerun parity check"
        ) from exc

    case_data = replay["case"]
    candidate_data = case_data.get("candidate")
    if not isinstance(candidate_data, dict) or _contains_redaction(candidate_data):
        raise ContractError("artifact does not contain a complete candidate configuration")
    invocation = (
        case_data.get("static_args", []),
        case_data.get("static_kwargs", {}),
        case_data.get("candidate_kwargs", {}),
    )
    if any(_contains_redaction(value) for value in invocation):
        raise ContractError(
            "candidate arguments were redacted; remove secrets from arguments and rerun parity check"
        )
    try:
        candidate_probe = {"candidate": json.loads(json.dumps(candidate_data))}
        _resolve_replay_paths(candidate_probe, project_root, sides=("candidate",))
    except Exception as exc:
        raise ContractError(
            "candidate project paths are no longer reconstructable; repair them and rerun "
            "parity check"
        ) from exc
    bundle = case_data.get("input_bundle")
    if bundle is None:
        binding: Literal["single", "keyword", "positional"] = "single"
    elif isinstance(bundle, dict) and bundle.get("binding") in {"keyword", "positional"}:
        binding = bundle["binding"]
    else:
        raise ContractError("artifact contains an invalid input binding")
    try:
        candidate = CallableSpec.model_validate(candidate_data)
        comparison = ComparisonPolicy.model_validate(case_data.get("comparison", {}))
        blueprint = DistilledCase(
            name=case_name,
            candidate=candidate,
            comparison=comparison,
            static_args=case_data.get("static_args", []),
            static_kwargs=case_data.get("static_kwargs", {}),
            candidate_kwargs=case_data.get("candidate_kwargs", {}),
            timeout_seconds=case_data.get("timeout_seconds", 30.0),
            input_binding=binding,
            examples=[
                ContractExample(
                    finding_signature=signature,
                    inputs=[
                        ContractInput(name=name, file=f"placeholder-{index}.arrow")
                        for index, name in enumerate(replay_inputs)
                    ],
                    expected=ContractExpectation(
                        outcome="returned",
                        metrics=RunMetrics(duration_seconds=0),
                        has_table=False,
                        has_value=False,
                        runtime=collect_runtime_provenance(),
                    ),
                )
            ],
        )
    except ValueError as exc:
        raise ContractError("artifact contains an invalid candidate contract") from exc

    expected_payload = _bound_json(root, manifest, "reference.json")
    output_payload = expected_payload.get("output")
    output_source: tuple[Path, dict[str, Any], Literal["arrow", "json"]] | None = None
    if output_payload is not None:
        if (
            not isinstance(output_payload, dict)
            or set(output_payload) != {"kind", "file"}
            or output_payload.get("kind") not in {"arrow", "json"}
            or not isinstance(output_payload.get("file"), str)
        ):
            raise ContractError("artifact contains an invalid reference output binding")
        kind: Literal["arrow", "json"] = output_payload["kind"]
        suffix = ".arrow" if kind == "arrow" else ".json"
        source, metadata = _source_file(root, manifest, output_payload["file"], suffix=suffix)
        output_source = source, metadata, kind
    try:
        expected = ContractExpectation.model_validate(expected_payload)
    except ValueError as exc:
        raise ContractError(
            "artifact has no exact semantic reference expectation; rerun parity check with "
            f"Parity {__version__}"
        ) from exc
    inputs = [
        (name, *_source_file(root, manifest, path.name, suffix=".arrow"))
        for name, path in replay_inputs.items()
    ]
    return {
        "root": root,
        "project_root": project_root,
        "blueprint": blueprint,
        "inputs": inputs,
        "expected": expected,
        "output_source": output_source,
    }


def _copy_verified(source: Path, destination: Path, metadata: dict[str, Any]) -> ContractFile:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
    except OSError as exc:
        raise ContractError("contract data could not be copied from its source artifact") from exc
    digest = _sha256(destination)
    size = destination.stat().st_size
    if digest != metadata.get("sha256") or size != metadata.get("bytes"):
        raise ContractError("source artifact changed while the contract was distilled")
    return ContractFile(sha256=digest, bytes=size)


def _contract_destination(destination: str | Path, project_root: Path) -> tuple[Path, int]:
    requested = Path(destination)
    if requested.exists() or requested.is_symlink():
        raise ContractError("contract destination already exists")
    absolute = Path(os.path.abspath(requested))
    resolved = absolute.resolve()
    try:
        relative = resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ContractError(
            "contract destination must be inside the recorded project root"
        ) from exc
    if not relative.parts:
        raise ContractError("contract destination must be below the recorded project root")
    if len(relative.parts) > 64:
        raise ContractError("contract destination is nested too deeply")
    return absolute, len(relative.parts)


def distill_contract(
    report: str | Path,
    destination: str | Path,
    *,
    artifact_root: str | Path | None = None,
) -> DistillationResult:
    """Create one atomic candidate-only contract from signed report findings."""

    from parity.evidence import (
        EvidenceError,
        _contained_artifact,
        _report_entries,
        _resolve_artifact_root,
    )

    report_path = Path(report)
    try:
        raw_report = report_path.read_bytes()
        report_payload = json.loads(raw_report)
    except (OSError, ValueError) as exc:
        raise ContractError("distillation report is missing or invalid") from exc
    if not isinstance(report_payload, dict):
        raise ContractError("distillation report must contain a JSON object")
    try:
        entries = _report_entries(report_payload)
        source_root = _resolve_artifact_root(entries, artifact_root, report=report_path)
    except EvidenceError as exc:
        raise ContractError(str(exc)) from exc

    # A finding signature describes one semantic difference.  Repeated report
    # rows for the same case and signature cannot add contract coverage.
    unique_entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for case_name, artifact_name, signature in entries:
        marker = (case_name, signature)
        if marker not in seen:
            unique_entries.append((case_name, artifact_name, signature))
            seen.add(marker)

    sources: list[dict[str, Any]] = []
    project_root: Path | None = None
    for case_name, artifact_name, signature in unique_entries:
        try:
            artifact = _contained_artifact(source_root, artifact_name)
        except EvidenceError as exc:
            raise ContractError(str(exc)) from exc
        source = _source_artifact(
            case_name=case_name,
            artifact=artifact,
            signature=signature,
        )
        if project_root is None:
            project_root = source["project_root"]
        elif source["project_root"] != project_root:
            raise ContractError("one distilled contract cannot span multiple project roots")
        sources.append(source)
    if project_root is None:  # pragma: no cover - report validation requires entries
        raise ContractError("report contains no distillable findings")

    destination_path, levels = _contract_destination(destination, project_root)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination_path.name}.pending-", dir=destination_path.parent)
    )
    try:
        (temporary / ".gitignore").write_text("*\n", encoding="utf-8")
        files: dict[str, ContractFile] = {}
        case_builders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for source in sources:
            blueprint: DistilledCase = source["blueprint"]
            builder = case_builders.get(blueprint.name)
            declaration = blueprint.model_dump(mode="json", exclude={"examples"})
            if builder is None:
                builder = {**declaration, "examples": []}
                case_builders[blueprint.name] = builder
            elif {key: value for key, value in builder.items() if key != "examples"} != declaration:
                raise ContractError(
                    "one report contains conflicting candidate contracts for a case"
                )
            case_index = list(case_builders).index(blueprint.name)
            example_index = len(builder["examples"])
            prefix = f"cases/{case_index:03d}/examples/{example_index:03d}"
            inputs: list[ContractInput] = []
            for input_index, (name, source_path, metadata) in enumerate(source["inputs"]):
                relative = f"{prefix}/input-{input_index:03d}.arrow"
                files[relative] = _copy_verified(source_path, temporary / relative, metadata)
                inputs.append(ContractInput(name=name, file=relative))
            expected: ContractExpectation = source["expected"]
            output_source = source["output_source"]
            output: ContractOutput | None = None
            if output_source is not None:
                source_path, metadata, kind = output_source
                extension = "arrow" if kind == "arrow" else "json"
                relative = f"{prefix}/expected.{extension}"
                files[relative] = _copy_verified(source_path, temporary / relative, metadata)
                output = ContractOutput(kind=kind, file=relative)
            expected_payload = expected.model_dump(mode="json")
            expected_payload["output"] = output.model_dump(mode="json") if output else None
            contract_example = ContractExample(
                finding_signature=blueprint.examples[0].finding_signature,
                inputs=inputs,
                expected=ContractExpectation.model_validate(expected_payload),
            )
            # Fail during distillation, not during the first future gate, when
            # an otherwise hash-valid source contains unreadable Arrow/JSON.
            _expected_observation(temporary, contract_example.expected)
            _bound_inputs(temporary, contract_example, blueprint.input_binding)
            builder["examples"].append(contract_example.model_dump(mode="json"))

        manifest = DistilledContractManifest(
            version=1,
            created_at=datetime.now(UTC),
            parity_version=__version__,
            source_report_sha256=hashlib.sha256(raw_report).hexdigest(),
            path_base=ContractPathBase(kind="contract_ancestor", levels=levels),
            contains_input_data=True,
            contains_output_data=True,
            cases=[DistilledCase.model_validate(case) for case in case_builders.values()],
            files=files,
        )
        (temporary / "contract.json").write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        if destination_path.exists() or destination_path.is_symlink():
            raise ContractError("contract destination already exists")
        temporary.rename(destination_path)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return DistillationResult(
        path=destination_path,
        cases=len(manifest.cases),
        examples=sum(len(case.examples) for case in manifest.cases),
        source_report_sha256=manifest.source_report_sha256,
    )


def _contract_root(path: Path) -> Path:
    if path.is_dir():
        return path
    if path.name == "contract.json":
        return path.parent
    raise ContractError("contract must be a directory or contract.json")


def _contained_file(root: Path, name: str) -> Path:
    current = root
    for part in PurePosixPath(_safe_file(name)).parts:
        current /= part
        if current.is_symlink():
            raise ContractError(f"contract data file is not a regular contained file: {name}")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"contract data file is missing: {name}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ContractError(f"contract data file is not a regular contained file: {name}")
    return resolved


def _load_contract(path: str | Path) -> tuple[Path, DistilledContractManifest, str]:
    declared = _contract_root(Path(path))
    if declared.is_symlink():
        raise ContractError("contract directory cannot be a symbolic link")
    try:
        root = declared.resolve(strict=True)
    except OSError as exc:
        raise ContractError("contract directory is missing or invalid") from exc
    contract_path = root / "contract.json"
    if contract_path.is_symlink() or not contract_path.is_file():
        raise ContractError("contract.json must be a regular file")
    try:
        raw = contract_path.read_text(encoding="utf-8")
        manifest = DistilledContractManifest.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise ContractError("contract.json is missing or invalid") from exc
    for name, metadata in manifest.files.items():
        file_path = _contained_file(root, name)
        try:
            size = file_path.stat().st_size
            digest = _sha256(file_path)
        except OSError as exc:
            raise ContractError(f"contract data file could not be read: {name}") from exc
        if size != metadata.bytes:
            raise ContractError(f"contract size check failed: {name}")
        if digest != metadata.sha256:
            raise ContractError(f"contract integrity check failed: {name}")
    return root, manifest, raw


def _execution_root(root: Path, path_base: ContractPathBase) -> Path:
    base = root
    for _ in range(path_base.levels):
        parent = base.parent
        if parent == base:
            raise ContractError("contract path base escapes the contract filesystem")
        base = parent
    if not base.is_dir():  # pragma: no cover - resolved ancestors are directories
        raise ContractError("contract project root is missing")
    return base


def _expected_observation(root: Path, expected: ContractExpectation) -> Observation:
    table = None
    value: JsonValue = None
    if expected.output is not None and expected.output.kind == "arrow":
        table = load_arrow_fixture(_contained_file(root, expected.output.file))
    elif expected.output is not None:
        try:
            value = json.loads(
                _contained_file(root, expected.output.file).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ContractError("stored reference JSON output is invalid") from exc
    exception = (
        ExceptionInfo.from_dict(expected.exception.model_dump(mode="json"))
        if expected.exception is not None
        else None
    )
    return Observation(
        outcome=(
            ExecutionOutcome.RETURNED if expected.outcome == "returned" else ExecutionOutcome.RAISED
        ),
        metrics=expected.metrics,
        table=table,
        value=value,
        has_value=expected.has_value,
        exception=exception,
        mutated_inputs=tuple(expected.mutated_inputs),
        return_type=expected.return_type,
        runtime=expected.runtime,
    )


def _bound_inputs(
    root: Path,
    example: ContractExample,
    binding: Literal["single", "keyword", "positional"],
) -> ArrowInputBundle:
    items = [
        (item.name, load_arrow_fixture(_contained_file(root, item.file))) for item in example.inputs
    ]
    if binding == "single":
        return items[0][1]
    if binding == "positional":
        return tuple(table for _name, table in items)
    return dict(items)


def _error_failure(source: str) -> ExampleResult:
    return ExampleResult(
        source=source,
        status=Status.ERROR,
        mismatches=[
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="candidate could not be executed (execution_error)",
                path="$candidate",
            )
        ],
    )


def _verify_case(root: Path, project_root: Path, case: DistilledCase) -> CaseResult:
    started = time.perf_counter()
    failures: list[ExampleResult] = []
    examples_run = 0
    reference_runtime = case.examples[0].expected.runtime
    candidate_runtime: RuntimeProvenance | None = None
    try:
        from parity.engine import _resolve_replay_paths, _restore_environment

        case_data: dict[str, Any] = {"candidate": case.candidate.model_dump(mode="json")}
        _restore_environment(case_data, sides=("candidate",))
        _resolve_replay_paths(case_data, project_root, sides=("candidate",))
        candidate = CallableSpec.model_validate(case_data["candidate"])
    except Exception:
        return CaseResult(
            name=case.name,
            status=Status.ERROR,
            examples_run=0,
            failures=[_error_failure("contract")],
            provenance=CaseProvenance(reference=reference_runtime, verification="captured"),
            elapsed_seconds=time.perf_counter() - started,
        )

    kwargs = {**case.static_kwargs, **case.candidate_kwargs}
    for index, example in enumerate(case.examples):
        examples_run += 1
        source = f"contract:{index + 1}"
        try:
            expected = _expected_observation(root, example.expected)
            inputs = _bound_inputs(root, example, case.input_binding)
            observed = execute_isolated(
                candidate,
                inputs,
                static_args=case.static_args,
                static_kwargs=kwargs,
                timeout_seconds=case.timeout_seconds,
            )
        except Exception:
            failures.append(_error_failure(source))
            break
        if observed.runtime is not None:
            candidate_runtime = observed.runtime
        if observed.outcome in {
            ExecutionOutcome.ERROR,
            ExecutionOutcome.TIMED_OUT,
            ExecutionOutcome.CRASHED,
        }:
            failures.append(
                ExampleResult(
                    source=source,
                    status=Status.ERROR,
                    mismatches=[
                        Mismatch(
                            kind=MismatchKind.EXCEPTION,
                            message=f"candidate could not be executed ({observed.outcome.value})",
                            path="$candidate",
                        )
                    ],
                    reference_metrics=expected.metrics,
                    candidate_metrics=observed.metrics,
                )
            )
            break
        mismatches = compare_observations(expected, observed, case.comparison)
        if mismatches:
            failures.append(
                ExampleResult(
                    source=source,
                    status=Status.FAILED,
                    mismatches=mismatches,
                    reference_metrics=expected.metrics,
                    candidate_metrics=observed.metrics,
                    finding_signature=mismatch_signature(mismatches),
                )
            )
    status = (
        Status.ERROR
        if any(failure.status is Status.ERROR for failure in failures)
        else Status.FAILED
        if failures
        else Status.PASSED
    )
    return CaseResult(
        name=case.name,
        status=status,
        examples_run=examples_run,
        deterministic_examples=examples_run,
        failures=failures,
        provenance=CaseProvenance(
            reference=reference_runtime,
            candidate=candidate_runtime,
            verification="captured",
        ),
        elapsed_seconds=time.perf_counter() - started,
    )


def verify_contract(path: str | Path) -> SuiteResult:
    """Execute only the candidate against every stored reference expectation."""

    started = time.perf_counter()
    root, manifest, raw = _load_contract(path)
    project_root = _execution_root(root, manifest.path_base)
    cases = [_verify_case(root, project_root, case) for case in manifest.cases]
    status = (
        Status.ERROR
        if any(case.status is Status.ERROR for case in cases)
        else Status.FAILED
        if any(case.status is Status.FAILED for case in cases)
        else Status.PASSED
    )
    return SuiteResult(
        status=status,
        cases=cases,
        elapsed_seconds=time.perf_counter() - started,
        parity_version=__version__,
        provenance=SuiteProvenance(
            orchestrator=collect_runtime_provenance(),
            config_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        ),
    )


__all__ = [
    "ContractError",
    "DistillationResult",
    "DistilledContractManifest",
    "distill_contract",
    "verify_contract",
]
