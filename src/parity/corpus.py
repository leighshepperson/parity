"""Read an exact corpus of complete JSON calls without inventing input combinations."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from parity.invocation import Invocation, ResolvedInvocation

_MAX_LINE_BYTES = 1024 * 1024
_MAX_CORPUS_BYTES = 64 * 1024 * 1024
_MAX_CALLS = 100_000


class CorpusError(ValueError):
    """A safe diagnostic that never includes call values or a local file path."""


@dataclass(frozen=True)
class CallCorpus:
    invocation: ResolvedInvocation
    sha256: str


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def load_calls(path: str | Path) -> CallCorpus:
    """Validate the whole JSONL corpus before any target is imported or executed.

    Each nonblank line is an object with optional ``args`` (array) and ``kwargs``
    (object). An empty object is a zero-argument call. Unknown fields, duplicate
    keys and non-finite numbers are errors. Existing Invocation bounds also apply.
    """

    calls: list[tuple[str, Invocation]] = []
    digest = hashlib.sha256()
    total_bytes = 0
    line_number = 0
    try:
        with Path(path).open("rb") as stream:
            while line := stream.readline(_MAX_LINE_BYTES + 1):
                line_number += 1
                total_bytes += len(line)
                if len(line) > _MAX_LINE_BYTES:
                    raise CorpusError(f"calls line {line_number} exceeds 1 MiB")
                if total_bytes > _MAX_CORPUS_BYTES:
                    raise CorpusError("calls file exceeds 64 MiB")
                digest.update(line)
                if not line.strip():
                    continue
                if len(calls) >= _MAX_CALLS:
                    raise CorpusError("calls file exceeds 100000 calls")
                try:
                    value = json.loads(
                        line.decode("utf-8"),
                        object_pairs_hook=_object,
                        parse_constant=_constant,
                        parse_float=_float,
                    )
                    if not isinstance(value, dict) or set(value) - {"args", "kwargs"}:
                        raise ValueError("expected a call object")
                    args, kwargs = value.get("args", []), value.get("kwargs", {})
                    if not isinstance(args, list) or not isinstance(kwargs, dict):
                        raise ValueError("invalid argument containers")
                    call = Invocation(args=tuple(args), kwargs=kwargs)
                except (ValueError, TypeError, RecursionError) as error:
                    raise CorpusError(
                        f"invalid call on line {line_number}: expected a JSON object with only "
                        "args (array) and kwargs (object), unique keys, finite numbers and "
                        "bounded JSON arguments"
                    ) from error
                calls.append((f"calls:line:{line_number}", call))
    except OSError as error:
        raise CorpusError(f"calls file could not be read ({type(error).__name__})") from error
    if not calls:
        raise CorpusError("calls file must contain at least one call")
    return CallCorpus(ResolvedInvocation(tuple(calls), None), digest.hexdigest())
