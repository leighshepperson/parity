"""Domain adapter for the legacy JavaScript rules engine."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path

from parity.target_adapter import AdapterError, CommandAdapter, RuntimeInfo, TargetRaised

PROGRAM = Path(__file__).resolve().parent / "legacy_rules.js"
NODE = shutil.which("node")
_VERSION = re.compile(r"^v?([A-Za-z0-9][A-Za-z0-9._+!~-]{0,62})$")


def _node_version() -> str:
    if NODE is None:
        return "unavailable"
    try:
        completed = subprocess.run(
            [NODE, "--version"],
            text=True,
            encoding="ascii",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    match = _VERSION.fullmatch(completed.stdout.strip())
    return match.group(1) if completed.returncode == 0 and match is not None else "unknown"


def _inspect() -> None:
    if NODE is None:
        raise AdapterError("target_unavailable", "Node.js executable is unavailable")
    try:
        node_metadata = Path(NODE).resolve(strict=True).stat()
        program_metadata = PROGRAM.resolve(strict=True).stat()
    except OSError:
        raise AdapterError(
            "target_unavailable", "legacy JavaScript target is unavailable"
        ) from None
    if not stat.S_ISREG(node_metadata.st_mode) or not stat.S_ISREG(program_metadata.st_mode):
        raise AdapterError("target_unavailable", "legacy JavaScript target is unavailable")


def _execute(program: object, context: object, *, threshold: object) -> object:
    if NODE is None:
        raise AdapterError("target_unavailable", "Node.js executable is unavailable")
    try:
        payload = json.dumps(
            {"program": program, "context": context, "threshold": threshold},
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise AdapterError(
            "invalid_canonical_input", "rules invocation is not bounded JSON"
        ) from error
    try:
        completed = subprocess.run(
            [NODE, str(PROGRAM)],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError(
            "target_invocation_failed", "legacy JavaScript target could not be invoked"
        ) from error
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 1024 * 1024:
        raise AdapterError(
            "invalid_target_output", "legacy JavaScript target returned an unusable response"
        )
    try:
        response = json.loads(completed.stdout)
    except ValueError as error:
        raise AdapterError(
            "invalid_target_output", "legacy JavaScript target returned invalid JSON"
        ) from error
    if not isinstance(response, dict):
        raise AdapterError(
            "invalid_target_output", "legacy JavaScript target response is not an object"
        )
    if set(response) == {"outcome", "value"} and response.get("outcome") == "returned":
        return response["value"]
    if set(response) == {"outcome", "message"} and response.get("outcome") == "raised":
        message = response.get("message")
        if not isinstance(message, str):
            raise AdapterError("invalid_target_output", "legacy JavaScript exception is invalid")
        raise TargetRaised(
            message,
            module="legacy.rules",
            exception_type="RuleEvaluationError",
        )
    raise AdapterError(
        "invalid_target_output", "legacy JavaScript target response has an invalid outcome"
    )


adapter = CommandAdapter(
    runtime=RuntimeInfo(name="node", version=_node_version()),
    inspect=_inspect,
    execute=_execute,
    return_type="legacy.rules.Result",
)
