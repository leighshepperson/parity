"""Atomic, replayable failure campaigns.

Artifacts are the sole place where Parity persists customer frame data.  Each
campaign is first completed in a private sibling directory and then atomically
renamed into place, so interrupted runs never leave a plausible partial result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from parity.execution import Observation, _write_arrow, redact_text
from parity.models import CallableSpec, CaseConfig, ExampleResult

_SECRET_KEY = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)


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


def _spec_for_replay(
    spec: CallableSpec | None, *, invocation_directory: Path
) -> dict[str, Any] | None:
    if spec is None:
        return None
    workdir: str | None = None
    if spec.workdir is not None:
        try:
            workdir = str(spec.workdir.resolve().relative_to(invocation_directory.resolve()))
        except ValueError:
            # Falling back to cwd can import a different same-named module and
            # silently turn a failure into a pass. Preserve the evidence, but
            # decline automatic replay when its import root cannot be recorded
            # without exposing an absolute host path.
            return None
    python: str | None = None
    if spec.python is not None:
        try:
            python = str(spec.python.resolve().relative_to(invocation_directory.resolve()))
        except ValueError:
            # As with import roots, substituting the current interpreter could
            # silently change dependency semantics. External interpreters make
            # the artifact evidence-only unless the user authors a config.
            return None
    return {
        "target": spec.target,
        "adapter": spec.adapter,
        # Replays inherit environment from the caller.  Recording even innocent
        # values makes accidental credential persistence much more likely.
        "python": python,
        "workdir": workdir,
        "environment": dict.fromkeys(sorted(spec.environment), "<required-from-environment>"),
    }


def _case_for_replay(
    case: str | CaseConfig,
    reference: CallableSpec | None,
    candidate: CallableSpec | None,
    *,
    invocation_directory: Path,
) -> dict[str, Any]:
    if isinstance(case, CaseConfig):
        config = case.model_dump(mode="json", by_alias=True)
        config["fixture"] = "input.arrow"
        config["reference"] = _spec_for_replay(
            case.reference, invocation_directory=invocation_directory
        )
        config["candidate"] = _spec_for_replay(
            case.candidate, invocation_directory=invocation_directory
        )
        config["static_kwargs"] = _sanitize_json(config.get("static_kwargs", {}))
        config["static_args"] = _sanitize_json(config.get("static_args", []))
        return config
    return {
        "name": case,
        "fixture": "input.arrow",
        "reference": _spec_for_replay(reference, invocation_directory=invocation_directory),
        "candidate": _spec_for_replay(candidate, invocation_directory=invocation_directory),
    }


def _result_payload(result: ExampleResult | BaseModel | Observation | dict[str, Any]) -> Any:
    if isinstance(result, Observation):
        return result.to_metadata()
    if isinstance(result, BaseModel):
        return _sanitize_json(result.model_dump(mode="json"))
    return _sanitize_json(result)


class ArtifactStore:
    """Write and inspect Parity failure campaigns beneath one root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write_failure(
        self,
        case_name: str | CaseConfig,
        input_table: pa.Table,
        result: ExampleResult | BaseModel | Observation | dict[str, Any],
        *,
        reference: CallableSpec | None = None,
        candidate: CallableSpec | None = None,
        source: str | None = None,
        seed: int | None = None,
    ) -> Path:
        """Persist one minimal failing input and return its campaign directory."""

        case = case_name
        name = case.name if isinstance(case, CaseConfig) else case
        safe_case = _safe_name(name)
        case_root = self.root / safe_case
        case_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=case_root))
        try:
            arrow_path = temporary / "input.arrow"
            parquet_path = temporary / "input.parquet"
            result_path = temporary / "result.json"
            replay_path = temporary / "replay.json"
            manifest_path = temporary / "manifest.json"
            _write_arrow(input_table, arrow_path)
            try:
                pq.write_table(input_table, parquet_path)
            except pa.ArrowNotImplementedError:
                # Arrow IPC is the lossless replay authority. Parquet is a
                # convenience copy and cannot represent every supported Arrow
                # schema (notably a struct with no child fields).
                parquet_path.unlink(missing_ok=True)
            result_path.write_text(
                json.dumps(_result_payload(result), indent=2, sort_keys=True, allow_nan=True)
                + "\n",
                encoding="utf-8",
            )
            replay = {
                "version": 1,
                "command": ["parity", "replay", "<artifact-path>"],
                "working_directory": "original invocation directory",
                "input": "input.arrow",
                "path_base": "invocation_cwd",
                "case": _case_for_replay(
                    case,
                    reference,
                    candidate,
                    invocation_directory=Path.cwd(),
                ),
                "environment": "inherited; values are never stored in artifacts",
            }
            replay_path.write_text(
                json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            input_hash = _sha256(arrow_path)
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
            evidence_paths = [arrow_path, result_path, replay_path]
            if parquet_path.exists():
                evidence_paths.insert(1, parquet_path)
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


def write_failure(
    root: str | Path,
    case_name: str | CaseConfig,
    input_table: pa.Table,
    result: ExampleResult | BaseModel | Observation | dict[str, Any],
    **kwargs: Any,
) -> Path:
    """Functional convenience wrapper around :class:`ArtifactStore`."""

    return ArtifactStore(root).write_failure(case_name, input_table, result, **kwargs)


__all__ = ["ArtifactStore", "write_failure"]
