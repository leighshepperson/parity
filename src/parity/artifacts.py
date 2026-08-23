"""Atomic, replayable failure campaigns.

Artifacts are the sole place where Parity persists invocation data. Frame leaves
remain private files; safe JSON arguments are retained in the replay document.
Each campaign is first completed in a private sibling directory and then
atomically renamed into place, so interrupted runs never leave a plausible
partial result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from parity.execution import Observation, _write_arrow, redact_text
from parity.invocation import FrameSequence, Invocation, InvocationValue, iter_frames
from parity.models import CallableSpec, CaseConfig, CaseProvenance, ExampleResult

_SECRET_KEY = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)

ArtifactInput: TypeAlias = Invocation


def _normalize_inputs(value: ArtifactInput) -> list[tuple[str, pa.Table]]:
    """Return every validated Arrow leaf in invocation order."""

    if not isinstance(value, Invocation):
        raise TypeError("artifact input must be a parity.Invocation")
    return [(item.path, item.table) for item in iter_frames(value)]


def _invocation_document(
    invocation: Invocation,
    input_files: Mapping[str, str],
) -> dict[str, Any]:
    """Serialize invocation shape while replacing Arrow leaves with artifact files."""

    def encode(value: InvocationValue, path: str) -> dict[str, Any]:
        if isinstance(value, pa.Table):
            return {"kind": "arrow", "file": input_files[path]}
        if isinstance(value, FrameSequence):
            return {
                "kind": "frames",
                "container": value.container,
                "items": [
                    encode(table, f"{path}/{index}") for index, table in enumerate(value.items)
                ],
            }
        return {"kind": "json", "value": _sanitize_json(value)}

    return {
        "args": [encode(value, f"args/{index}") for index, value in enumerate(invocation.args)],
        "kwargs": {
            name: encode(value, f"kwargs/{name}") for name, value in invocation.kwargs.items()
        },
    }


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-")
    return safe[:100] or "case"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_json(value: Any, *, key: str | None = None) -> Any:
    if key and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): _sanitize_json(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, (type(None), bool, int, float)):
        return value
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return "<redacted>" in value or "<path>" in value
    if isinstance(value, dict):
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redaction(item) for item in value)
    return False


def _case_supports_automatic_replay(case: Mapping[str, Any]) -> bool:
    if not all(isinstance(case.get(side), dict) for side in ("reference", "candidate")):
        return False
    if any(_contains_redaction(case.get(side)) for side in ("reference", "candidate")):
        return False
    return isinstance(case.get("invocation"), dict)


def _spec_for_replay(
    spec: CallableSpec | None, *, invocation_directory: Path
) -> tuple[dict[str, Any] | None, str | None]:
    if spec is None:
        return None, "live_callable"
    workdir: str | None = None
    if spec.workdir is not None:
        try:
            workdir = str(spec.workdir.resolve().relative_to(invocation_directory.resolve()))
        except ValueError:
            # Falling back to cwd can import a different same-named module and
            # silently turn a failure into a pass. Preserve the evidence, but
            # decline automatic replay when its import root cannot be recorded
            # without exposing an absolute host path.
            return None, "external_workdir"
    python: str | None = None
    if spec.python is not None:
        try:
            # Preserve the configured virtual-environment entry point rather
            # than dereferencing it to a shared base Python executable.
            python = str(
                Path(os.path.abspath(spec.python)).relative_to(
                    Path(os.path.abspath(invocation_directory))
                )
            )
        except ValueError:
            # As with import roots, substituting the current interpreter could
            # silently change dependency semantics. External interpreters make
            # the artifact evidence-only unless the user authors a config.
            return None, "external_python"
    command: list[str] | None = None
    if spec.command is not None:
        command = []
        redact_next = False
        invocation_root = invocation_directory.resolve()
        launch_root = spec.workdir.resolve() if spec.workdir is not None else invocation_root
        for index, argument in enumerate(spec.command):
            if redact_next:
                command.append("<redacted>")
                redact_next = False
                continue
            sanitized = redact_text(argument)
            executable = Path(argument)
            path_like_executable = index == 0 and (
                executable.is_absolute()
                or argument.startswith(".")
                or os.sep in argument
                or (os.altsep is not None and os.altsep in argument)
            )
            if path_like_executable:
                resolved_executable = (
                    executable.resolve()
                    if executable.is_absolute()
                    else (launch_root / executable).resolve()
                )
                try:
                    resolved_executable.relative_to(invocation_root)
                except ValueError:
                    # Never persist an external host path or substitute another
                    # executable during replay.
                    return None, "external_command"
                if not resolved_executable.is_file():
                    return None, "missing_command"
                sanitized = os.path.relpath(resolved_executable, launch_root)
                if os.sep not in sanitized and (os.altsep is None or os.altsep not in sanitized):
                    sanitized = f".{os.sep}{sanitized}"
            if "=" in argument:
                key, _separator, _value = argument.partition("=")
                if _SECRET_KEY.search(key):
                    sanitized = f"{key}=<redacted>"
            elif _SECRET_KEY.search(argument.lstrip("-")):
                redact_next = True
            command.append(sanitized)
    return (
        {
            "target": spec.target,
            "command": command,
            "canonicalizer": spec.canonicalizer,
            "adapter": spec.adapter,
            "pandas_input": spec.pandas_input,
            "record_distributions": spec.record_distributions,
            "required_distributions": spec.required_distributions,
            "native_threads": spec.native_threads,
            # Replays inherit environment from the caller.  Recording even innocent
            # values makes accidental credential persistence much more likely.
            "python": python,
            "workdir": workdir,
            "environment": dict.fromkeys(sorted(spec.environment), "<required-from-environment>"),
        },
        None,
    )


def _specs_for_replay(
    reference: CallableSpec | None,
    candidate: CallableSpec | None,
    *,
    invocation_directory: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, str]]:
    endpoints: dict[str, dict[str, Any] | None] = {}
    blockers: dict[str, str] = {}
    for side, spec in (("reference", reference), ("candidate", candidate)):
        endpoint, blocker = _spec_for_replay(spec, invocation_directory=invocation_directory)
        endpoints[side] = endpoint
        if blocker is not None:
            blockers[side] = blocker
    return endpoints["reference"], endpoints["candidate"], blockers


def _case_for_replay(
    case: str | CaseConfig,
    reference: CallableSpec | None,
    candidate: CallableSpec | None,
    *,
    invocation_directory: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    if isinstance(case, CaseConfig):
        config = case.model_dump(mode="json", by_alias=True)
        generation = config.get("generation")
        if isinstance(generation, dict):
            # The exact Arrow witness replaces project code as the replay input
            # authority. Replaying must not import or run the generator again.
            generation["generator"] = None
        # The artifact's separately integrity-bound invocation is authoritative.
        # An empty declaration keeps CaseConfig structurally valid without
        # re-running project generation or loading the original fixtures.
        config["invocation"] = {}
        reference_config, candidate_config, blockers = _specs_for_replay(
            case.reference,
            case.candidate,
            invocation_directory=invocation_directory,
        )
        config["reference"] = reference_config
        config["candidate"] = candidate_config
        return config, blockers
    reference_config, candidate_config, blockers = _specs_for_replay(
        reference,
        candidate,
        invocation_directory=invocation_directory,
    )
    replay_case: dict[str, Any] = {
        "name": case,
        "reference": reference_config,
        "candidate": candidate_config,
    }
    replay_case["invocation"] = {}
    return replay_case, blockers


def _result_payload(result: ExampleResult | BaseModel | Observation | dict[str, Any]) -> Any:
    if isinstance(result, Observation):
        return result.to_metadata()
    if isinstance(result, BaseModel):
        return _sanitize_json(result.model_dump(mode="json"))
    return _sanitize_json(result)


def _write_reference_observation(root: Path, observation: Observation) -> list[Path]:
    """Persist the exact reference expectation beside one private finding witness."""

    metadata = observation.to_metadata()
    output: dict[str, str] | None = None
    paths: list[Path] = []
    if observation.table is not None:
        output_path = root / "reference.arrow"
        _write_arrow(observation.table, output_path)
        output = {"kind": "arrow", "file": output_path.name}
        paths.append(output_path)
    elif observation.has_value:
        output_path = root / "reference-value.json"
        output_path.write_text(
            json.dumps(observation.value, sort_keys=True, allow_nan=True) + "\n",
            encoding="utf-8",
        )
        output = {"kind": "json", "file": output_path.name}
        paths.append(output_path)
    metadata["output"] = output
    metadata_path = root / "reference.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    return [metadata_path, *paths]


class ArtifactStore:
    """Write and inspect Parity failure campaigns beneath one root."""

    def __init__(
        self,
        root: str | Path,
        *,
        invocation_directory: str | Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.invocation_directory = (
            Path(invocation_directory).resolve()
            if invocation_directory is not None
            else Path.cwd().resolve()
        )

    def write_failure(
        self,
        case_name: str | CaseConfig,
        input_table: ArtifactInput,
        result: ExampleResult | BaseModel | Observation | dict[str, Any],
        *,
        reference: CallableSpec | None = None,
        candidate: CallableSpec | None = None,
        source: str | None = None,
        seed: int | None = None,
        runtime_provenance: CaseProvenance | None = None,
        reference_observation: Observation | None = None,
        config_sha256: str | None = None,
    ) -> Path:
        """Persist one minimal failing invocation and return its campaign directory."""

        if config_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", config_sha256):
            raise ValueError("config_sha256 must be a lowercase SHA-256 digest")

        self.root.mkdir(parents=True, exist_ok=True)
        ignore = self.root / ".gitignore"
        try:
            with ignore.open("x", encoding="utf-8") as stream:
                # A private artifact root can contain compared values. Make it
                # self-ignoring even when the consumer repository has no root
                # .gitignore, without modifying an existing user policy.
                stream.write("*\n")
        except FileExistsError:
            pass

        case = case_name
        name = case.name if isinstance(case, CaseConfig) else case
        safe_case = _safe_name(name)
        case_root = self.root / safe_case
        case_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=case_root))
        try:
            normalized = _normalize_inputs(input_table)
            input_files: dict[str, str] = {}
            arrow_paths: list[Path] = []
            parquet_paths: list[Path] = []
            for index, (input_name, table) in enumerate(normalized):
                stem = f"input-{index:03d}"
                arrow_path = temporary / f"{stem}.arrow"
                parquet_path = temporary / f"{stem}.parquet"
                _write_arrow(table, arrow_path)
                arrow_paths.append(arrow_path)
                input_files[input_name] = arrow_path.name
                try:
                    pq.write_table(table, parquet_path)
                except pa.ArrowNotImplementedError:
                    # Arrow IPC is the lossless replay authority. Parquet is a
                    # convenience copy and cannot represent every Arrow schema.
                    parquet_path.unlink(missing_ok=True)
                else:
                    parquet_paths.append(parquet_path)
            result_path = temporary / "result.json"
            replay_path = temporary / "replay.json"
            manifest_path = temporary / "manifest.json"
            result_path.write_text(
                json.dumps(_result_payload(result), indent=2, sort_keys=True, allow_nan=True)
                + "\n",
                encoding="utf-8",
            )
            reference_paths = (
                _write_reference_observation(temporary, reference_observation)
                if reference_observation is not None
                else []
            )
            complete_runtime = bool(
                runtime_provenance is not None
                and runtime_provenance.reference is not None
                and runtime_provenance.candidate is not None
                and config_sha256 is not None
            )
            replay_case, replay_blockers = _case_for_replay(
                case,
                reference,
                candidate,
                invocation_directory=self.invocation_directory,
            )
            invocation_document = _invocation_document(input_table, input_files)
            if _contains_redaction(invocation_document):
                # JSON call arguments can contain credentials or host-local paths.
                # Keep the evidence safe and fail closed instead of pretending the
                # sanitized invocation can reproduce the original call.
                replay_blockers["artifact"] = "redacted_invocation"
            input_digest = hashlib.sha256()
            input_digest.update(
                json.dumps(
                    invocation_document,
                    allow_nan=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for input_name, arrow_path in zip(input_files, arrow_paths, strict=True):
                input_digest.update(input_name.encode("utf-8"))
                input_digest.update(b"\0")
                input_digest.update(bytes.fromhex(_sha256(arrow_path)))
            input_hash = input_digest.hexdigest()
            campaign_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + input_hash[:12]
            destination = case_root / campaign_id
            # Microsecond timestamps should be unique; retain atomicity even under
            # a frozen test clock by adding a numeric suffix before the rename.
            suffix = 1
            while destination.exists():
                destination = case_root / f"{campaign_id}-{suffix}"
                suffix += 1

            # Replay paths are rooted at the configuration directory captured by
            # this store, never at the directory of a future replay invocation.
            # Persist only a bounded ancestor count: it is path-free, portable
            # across project moves, and cannot name a sibling outside the project.
            path_base: dict[str, Any] | None = None
            try:
                relative_destination = destination.resolve().relative_to(self.invocation_directory)
            except ValueError:
                if complete_runtime:
                    replay_blockers["artifact"] = "external_artifact_root"
            else:
                levels = len(relative_destination.parts)
                if levels < 1:  # pragma: no cover - a campaign is always nested
                    raise RuntimeError("artifact campaign must be below its replay root")
                path_base = {"kind": "artifact_ancestor", "levels": levels}
            replay: dict[str, Any] = {
                "version": 3,
                "case": replay_case,
                "environment": "inherited; values are never stored in artifacts",
                "invocation": invocation_document,
            }
            if path_base is not None:
                replay["path_base"] = path_base
            if replay_blockers:
                # Preserve only a bounded reason code, never an external path.
                # This lets replay explain why reconstruction was declined
                # without leaking host layout into retained evidence.
                replay["replay_blockers"] = replay_blockers
            if runtime_provenance is not None:
                replay["expected_runtime"] = runtime_provenance.model_dump(mode="json")
            if config_sha256 is not None:
                replay["config_sha256"] = config_sha256
            if (
                complete_runtime
                and _case_supports_automatic_replay(replay_case)
                and not replay_blockers
            ):
                replay["command"] = ["parity", "replay", "<artifact-path>"]
            replay_path.write_text(
                json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest: dict[str, Any] = {
                "version": 3,
                "campaign_id": campaign_id,
                "case": name,
                "created_at": datetime.now(UTC).isoformat(),
                "source": redact_text(source) if source else None,
                "seed": seed,
                "contains_input_data": True,
                "files": {},
            }
            evidence_paths = [
                *arrow_paths,
                *parquet_paths,
                result_path,
                replay_path,
                *reference_paths,
            ]
            for path in evidence_paths:
                manifest["files"][path.name] = {
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(temporary, destination)
            return destination
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


__all__ = ["ArtifactStore"]
