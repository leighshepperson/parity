"""Privacy-safe, stable semantics for observed exceptions.

Exception text is useful behavioural evidence, but raw text often contains the
particular witness, temporary paths, object addresses, or dependency versions.
This module reduces that text to a stable pattern and an opaque fingerprint.
It deliberately does not retain tracebacks or arbitrary exception attributes.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_SEMANTICS_VERSION = "ex1"
_MAX_MESSAGE_CHARS = 16_384
_MAX_PATTERN_CHARS = 4_096
_MAX_STRUCTURED_ITEMS = 32
_MAX_TOKEN_CHARS = 128

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL)"
    r"[A-Z0-9_]*)\s*=\s*([^\s,;]+)"
)
_ABSOLUTE_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/][^\s:'\"]+|/(?:[^\s/'\"]+/)+[^\s:'\"]*)")
_URL = re.compile(r"(?i)\b(?:https?|file)://[^\s)>\]}]+")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_UUID = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])"
)
_ADDRESS = re.compile(r"(?i)\b0x[0-9a-f]{6,}\b")
_TIMESTAMP = re.compile(
    r"(?ix)\b(?:"
    r"\d{4}-\d{2}-\d{2}[t\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r")\b"
)
_VERSION = re.compile(
    r"(?ix)(?<![\w.])v?\d+\.\d+(?:\.\d+){0,3}"
    r"(?:[-+][0-9a-z][0-9a-z.-]*)?(?![\w.])"
)
_LONG_HEX_ID = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{16,}(?![0-9a-f])")
_LONG_DECIMAL_ID = re.compile(r"(?<!\w)\d{7,}(?!\w)")
_NUMBER = re.compile(
    r"(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?(?![\w.])",
    re.IGNORECASE,
)
_QUOTED_VALUE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`")
_OBJECT_REPR = re.compile(r"<([A-Za-z_][\w.]*) object at <address>>")
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_API_TOKEN = re.compile(r"\b(?:np|numpy|ndarray)\.[A-Za-z_][A-Za-z0-9_]*\b")
_SAFE_API_TOKEN = re.compile(r"^(?:np|numpy|ndarray)\.[A-Za-z_][A-Za-z0-9_]*$")
_CONTEXT_TOKEN = re.compile(
    r"(?i)\b(?:attribute|keyword(?: argument)?|method|function|alias|name)\s+"
    r"['\"`]([A-Za-z_][A-Za-z0-9_]*)['\"`]"
)
_ERROR_CODE = re.compile(r"(?i)\btype=([a-z][a-z0-9_.-]*)")
_BOOLEAN_OPTION = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(True|False|None)\b")
_SAFE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_LOCATION_SHAPE = re.compile(r"^(?:root|(?:field|index|item)(?:/(?:field|index|item)){0,15})$")
_MESSAGE_FINGERPRINT = re.compile(r"^ex1:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MessageSemantics:
    """Normalized exception message plus an opaque semantic fingerprint."""

    pattern: str
    fingerprint: str


def is_message_fingerprint(value: object) -> bool:
    """Return whether a value has the current opaque message-fingerprint form."""

    return isinstance(value, str) and _MESSAGE_FINGERPRINT.fullmatch(value) is not None


def redact_exception_text(text: str) -> str:
    """Remove common secrets and machine-local paths from exception text."""

    value = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", str(text))
    return _ABSOLUTE_PATH.sub("<path>", value)


def _semantic_tokens(text: str) -> tuple[str, ...]:
    tokens = {match.group(0).casefold() for match in _API_TOKEN.finditer(text)}
    tokens.update(f"name:{match.group(1).casefold()}" for match in _CONTEXT_TOKEN.finditer(text))
    tokens.update(f"type:{match.group(1).casefold()}" for match in _ERROR_CODE.finditer(text))
    tokens.update(
        f"option:{match.group(1).casefold()}={match.group(2).casefold()}"
        for match in _BOOLEAN_OPTION.finditer(text)
        if match.group(1).casefold() not in {"input_value", "input_type"}
    )
    return tuple(sorted(token[:_MAX_TOKEN_CHARS] for token in tokens))


def normalize_exception_message(text: str) -> MessageSemantics:
    """Return a bounded message pattern stable across irrelevant run details.

    The public pattern removes witness literals and volatile process metadata.
    API/error-code tokens participate in the fingerprint before quoted values
    are removed, so unrelated removals such as ``np.float_`` and ``np.cast`` do
    not collapse into one finding.
    """

    value = unicodedata.normalize("NFKC", redact_exception_text(text))[:_MAX_MESSAGE_CHARS]
    # Truncation can cut a quoted witness before its closing delimiter. Remove
    # such a tail instead of retaining a large prefix of private input data.
    for quote in ("'", '"', "`"):
        if value.count(quote) % 2:
            value = value[: value.rfind(quote)] + "<value>"
    value = _CONTROL.sub(" ", _ANSI_ESCAPE.sub("", value))
    tokens = _semantic_tokens(value)

    # Pydantic v1/v2 messages contain model/field/input values, but their
    # machine-readable ``type=...`` codes are the stable semantic distinction.
    validation_codes = sorted(token for token in tokens if token.startswith("type:"))
    if validation_codes and "validation error" in value.casefold():
        pattern = "validation error [" + ",".join(validation_codes) + "]"
    else:
        value = _URL.sub("<url>", value)
        value = _EMAIL.sub("<email>", value)
        value = _UUID.sub("<uuid>", value)
        value = _ADDRESS.sub("<address>", value)
        value = _OBJECT_REPR.sub(r"<\1 object at <address>>", value)
        value = _TIMESTAMP.sub("<timestamp>", value)
        value = _VERSION.sub("<version>", value)
        value = _LONG_HEX_ID.sub("<id>", value)
        value = _LONG_DECIMAL_ID.sub("<id>", value)
        value = _QUOTED_VALUE.sub("<value>", value)
        value = _NUMBER.sub("<number>", value)
        pattern = _WHITESPACE.sub(" ", value).strip().casefold()
    if not pattern:
        pattern = "<empty>"
    elif len(pattern) > _MAX_PATTERN_CHARS:
        pattern = pattern[:_MAX_PATTERN_CHARS].rstrip() + " <truncated>"

    encoded = json.dumps(
        {"version": _SEMANTICS_VERSION, "pattern": pattern, "tokens": tokens},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return MessageSemantics(
        pattern=pattern,
        fingerprint=f"{_SEMANTICS_VERSION}:{hashlib.sha256(encoded).hexdigest()}",
    )


def _safe_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= _MAX_TOKEN_CHARS and _SAFE_TOKEN.fullmatch(value) else None


def _safe_location_shape(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= _MAX_TOKEN_CHARS and _LOCATION_SHAPE.fullmatch(value) else None


def _safe_api_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= _MAX_TOKEN_CHARS and _SAFE_API_TOKEN.fullmatch(value) else None


def _location_shape(value: object) -> str | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    parts: list[str] = []
    for item in value[:16]:
        if isinstance(item, int):
            parts.append("index")
        elif isinstance(item, str):
            parts.append("field")
        else:
            parts.append("item")
    return "/".join(parts) or "root"


def extract_exception_details(error: BaseException) -> dict[str, Any]:
    """Extract a small allow-listed set of non-value exception metadata."""

    details: dict[str, Any] = {}
    api_tokens = {match.group(0)[:_MAX_TOKEN_CHARS] for match in _API_TOKEN.finditer(str(error))}
    if api_tokens:
        details["api_tokens"] = sorted(api_tokens)
    if (
        isinstance(error, OSError)
        and isinstance(error.errno, int)
        and -(2**31) <= error.errno < 2**31
    ):
        details["errno"] = error.errno

    module = type(error).__module__
    errors_method = getattr(error, "errors", None)
    if module.startswith(("pydantic", "pydantic_core")) and callable(errors_method):
        try:
            raw_errors = errors_method(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        except TypeError:  # Pydantic v1 does not support the privacy flags.
            try:
                raw_errors = errors_method()
            except Exception:
                raw_errors = None
        except Exception:
            raw_errors = None
        if isinstance(raw_errors, Sequence) and not isinstance(raw_errors, (str, bytes, bytearray)):
            codes: set[str] = set()
            locations: set[str] = set()
            for item in raw_errors[:_MAX_STRUCTURED_ITEMS]:
                if not isinstance(item, Mapping):
                    continue
                if (code := _safe_code(item.get("type", ""))) is not None:
                    codes.add(code)
                if (shape := _location_shape(item.get("loc"))) is not None:
                    locations.add(shape)
            if codes:
                details["error_codes"] = sorted(codes)
            if locations:
                details["location_shapes"] = sorted(locations)

    if isinstance(error, BaseExceptionGroup):
        members = {
            f"{type(member).__module__}.{type(member).__qualname__}"
            for member in error.exceptions[:_MAX_STRUCTURED_ITEMS]
        }
        details["member_types"] = sorted(members)
    return details


def normalize_exception_details(value: object) -> dict[str, Any]:
    """Validate exception details arriving from an isolated worker."""

    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    errno = value.get("errno")
    if isinstance(errno, int) and not isinstance(errno, bool) and -(2**31) <= errno < 2**31:
        normalized["errno"] = errno
    for key in ("api_tokens", "error_codes", "location_shapes", "member_types"):
        raw = value.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            continue
        if key == "location_shapes":
            normalizer = _safe_location_shape
        elif key == "api_tokens":
            normalizer = _safe_api_token
        else:
            normalizer = _safe_code
        items = {
            text for item in raw[:_MAX_STRUCTURED_ITEMS] if (text := normalizer(item)) is not None
        }
        if items:
            normalized[key] = sorted(items)
    return normalized


def exception_fingerprint(
    module: str | None,
    type_name: str,
    message_fingerprint: str,
    details: Mapping[str, Any],
) -> str:
    """Fingerprint one semantic Raise outcome without embedding private text."""

    encoded = json.dumps(
        {
            "version": _SEMANTICS_VERSION,
            "module": module or "",
            "type": type_name,
            "message": message_fingerprint,
            "details": normalize_exception_details(details),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{_SEMANTICS_VERSION}:{hashlib.sha256(encoded).hexdigest()}"
