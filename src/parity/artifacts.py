"""Atomic, replayable failure campaigns.

Artifacts are the sole place where Parity persists input frame data.  Each
campaign is first completed in a private sibling directory and then atomically
renamed into place, so interrupted runs never leave a plausible partial result.
"""

from __future__ import annotations

import hashlib
import json
import keyword
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
from parity.models import CallableSpec, CaseConfig, CaseProvenance, ExampleResult

_SECRET_KEY = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)

ArtifactInput: TypeAlias = pa.Table | Mapping[str, pa.Table]


def _normalize_inputs(value: ArtifactInput) -> tuple[list[tuple[str, pa.Table]], bool]:
    """Return ordered, validated inputs and whether this is a single-table campaign."""

    if isinstance(value, pa.Table):
        return [("input", value)], True
    if isinstance(value, Mapping):
        items = list(value.items())
        if any(
            not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name)
            for name, _ in items
        ):
            raise TypeError("input bundle names must be valid Python identifiers")
        if any(not isinstance(table, pa.Table) for _, table in items):
            raise TypeError("every bundled input must be a pyarrow.Table")
        if not 2 <= len(items) <= 3:
            raise ValueError("an input bundle must contain two or three named tables")
        return items, False
    raise TypeError("input must be an Arrow table or a map of two or three named tables")


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


def _case_supports_automatic_replay(
    case: Mapping[str, Any], *, input_files: Mapping[str, str], single_input: bool
) -> bool:
    if not all(isinstance(case.get(side), dict) for side in ("reference", "candidate")):
        return False
    if any(_contains_redaction(case.get(side)) for side in ("reference", "candidate")):
        return False
    invocation_arguments = (
        case.get("static_args"),
        case.get("static_kwargs"),
        case.get("reference_kwargs"),
        case.get("candidate_kwargs"),
    )
    if any(_contains_redaction(value) for value in invocation_arguments):
        return False
    if single_input:
        return case.get("fixture") == next(iter(input_files.values()))
    bundle = case.get("input_bundle")
    return (
        isinstance(bundle, dict)
        and isinstance(bundle.get("inputs"), dict)
        and set(bundle["inputs"]) == set(input_files)
    )


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
    input_files: Mapping[str, str],
    single_input: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    if isinstance(case, CaseConfig):
        config = case.model_dump(mode="json", by_alias=True)
        generation = config.get("generation")
        if isinstance(generation, dict):
            # The exact Arrow witness replaces project code as the replay input
            # authority. Replaying must not import or run the generator again.
            generation["generator"] = None
        if single_input:
            config["fixture"] = next(iter(input_files.values()))
            config["schema"] = None
            config["input_bundle"] = None
        else:
            bundle = config.get("input_bundle")
            if bundle is None and case.generation.generator is not None:
                bundle = {
                    "binding": "keyword",
                    "inputs": {
                        name: {"fixture": filename} for name, filename in input_files.items()
                    },
                    "relationships": [],
                }
                config["fixture"] = None
                config["schema"] = None
                config["input_bundle"] = bundle
            if not isinstance(bundle, dict) or not isinstance(bundle.get("inputs"), dict):
                # A direct ArtifactStore caller can persist a bundle without a
                # configured campaign. Keep the evidence, but make the replay
                # contract visibly non-reconstructable instead of guessing.
                config["input_bundle"] = None
            else:
                for name, filename in input_files.items():
                    input_spec = bundle["inputs"].get(name)
                    if not isinstance(input_spec, dict):
                        config["input_bundle"] = None
                        break
                    input_spec["fixture"] = filename
        reference_config, candidate_config, blockers = _specs_for_replay(
            case.reference,
            case.candidate,
            invocation_directory=invocation_directory,
        )
        config["reference"] = reference_config
        config["candidate"] = candidate_config
        config["static_kwargs"] = _sanitize_json(config.get("static_kwargs", {}))
        config["reference_kwargs"] = _sanitize_json(config.get("reference_kwargs", {}))
        config["candidate_kwargs"] = _sanitize_json(config.get("candidate_kwargs", {}))
        config["static_args"] = _sanitize_json(config.get("static_args", []))
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
    if single_input:
        replay_case["fixture"] = next(iter(input_files.values()))
    else:
        replay_case["input_bundle"] = {
            "binding": "keyword",
            "inputs": {name: {"fixture": filename} for name, filename in input_files.items()},
            "relationships": [],
        }
    return replay_case, blockers


def _result_payload(result: ExampleResult | BaseModel | Observation | dict[str, Any]) -> Any:
    if isinstance(result, Observation):
        return result.to_metadata()
    if isinstance(result, BaseModel):
        return _sanitize_json(result.model_dump(mode="json"))
    return _sanitize_json(result)


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
        config_sha256: str | None = None,
    ) -> Path:
        """Persist one minimal failing input bundle and return its campaign directory."""

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
            normalized, single_input = _normalize_inputs(input_table)
            if isinstance(case, CaseConfig):
                if single_input and case.input_bundle is not None:
                    raise ValueError("a bundled case requires all configured input tables")
                if (
                    not single_input
                    and case.input_bundle is None
                    and case.generation.generator is None
                ):
                    raise ValueError("a single-input case cannot store an input bundle")
                if case.input_bundle is not None:
                    expected_names = tuple(case.input_bundle.inputs)
                    supplied_names = tuple(name for name, _ in normalized)
                    if supplied_names != expected_names:
                        raise ValueError(
                            "artifact input names and order must exactly match the configured bundle"
                        )
            input_files: dict[str, str] = {}
            arrow_paths: list[Path] = []
            parquet_paths: list[Path] = []
            for index, (input_name, table) in enumerate(normalized):
                stem = "input" if single_input else f"input-{index:03d}"
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
                input_files=input_files,
                single_input=single_input,
            )
            replay: dict[str, Any] = {
                # The current replay transport covers both single inputs and
                # named bundles. A failure without complete bindings remains
                # useful inspection evidence, but cannot execute automatically.
                "version": 1,
                "working_directory": "recorded project configuration directory",
                "path_base": "invocation_cwd",
                "case": replay_case,
                "environment": "inherited; values are never stored in artifacts",
                "inputs": [
                    {"name": name, "file": filename} for name, filename in input_files.items()
                ],
            }
            if replay_blockers:
                # Preserve only a bounded reason code, never an external path.
                # This lets replay explain why reconstruction was declined
                # without leaking host layout into retained evidence.
                replay["replay_blockers"] = replay_blockers
            if runtime_provenance is not None:
                replay["expected_runtime"] = runtime_provenance.model_dump(mode="json")
            if config_sha256 is not None:
                replay["config_sha256"] = config_sha256
            if complete_runtime and _case_supports_automatic_replay(
                replay_case, input_files=input_files, single_input=single_input
            ):
                replay["command"] = ["parity", "replay", "<artifact-path>"]
            replay_path.write_text(
                json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            input_digest = hashlib.sha256()
            for input_name, arrow_path in zip(input_files, arrow_paths, strict=True):
                input_digest.update(input_name.encode("utf-8"))
                input_digest.update(b"\0")
                input_digest.update(bytes.fromhex(_sha256(arrow_path)))
            input_hash = input_digest.hexdigest()
            campaign_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + input_hash[:12]
            manifest: dict[str, Any] = {
                "version": 1,
                "campaign_id": campaign_id,
                "case": name,
                "created_at": datetime.now(UTC).isoformat(),
                "source": redact_text(source) if source else None,
                "seed": seed,
                "contains_input_data": True,
                "files": {},
            }
            evidence_paths = [*arrow_paths, *parquet_paths, result_path, replay_path]
            for path in evidence_paths:
                manifest["files"][path.name] = {
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            destination = case_root / campaign_id
            # Microsecond timestamps should be unique; retain atomicity even under
            # a frozen test clock by adding a numeric suffix before the rename.
            suffix = 1
            while destination.exists():
                destination = case_root / f"{campaign_id}-{suffix}"
                suffix += 1
            os.replace(temporary, destination)
            return destination
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


__all__ = ["ArtifactStore"]
