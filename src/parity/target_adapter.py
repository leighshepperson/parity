"""Supported SDK for implementing Parity command targets.

An adapter written with this module only owns the domain boundary: converting
canonical Arrow inputs into a legacy invocation and converting the result back.
The SDK owns target protocol v1, provenance, bounded transport, publication and
the distinction between semantic rejections and adapter failures.
"""

from __future__ import annotations

import importlib.metadata
import json
import keyword
import os
import platform
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

import pyarrow as pa
import pyarrow.ipc as ipc

from parity._version import __version__
from parity.exception_semantics import normalize_exception_details, redact_exception_text
from parity.provenance import (
    DistributionProvenance,
    RuntimeProvenance,
    normalize_distribution_names,
)

_PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_JSON_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_ARROW_OUTPUT_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_CALL_TOKEN = re.compile(r"^call-[0-9]{8}-[0-9a-f]{32}$")
_SAFE_EXCEPTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_SAFE_RUNTIME_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!~-]{0,127}$")
_SDK_DISTRIBUTIONS = ("parity-check", "pyarrow")


class _ProtocolFailure(RuntimeError):
    """A deliberately silent target-protocol or security failure."""


class AdapterError(RuntimeError):
    """A data-safe infrastructure failure raised deliberately by an adapter."""

    def __init__(self, code: str, safe_message: str) -> None:
        if not isinstance(code, str) or _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("adapter error codes must be stable identifier tokens")
        if not isinstance(safe_message, str) or not safe_message:
            raise ValueError("adapter errors require a non-empty safe message")
        self.code = code
        self.safe_message = redact_exception_text(safe_message)[:16_384]
        super().__init__(self.safe_message)


class TargetRaised(Exception):
    """An explicit semantic rejection produced by the wrapped target."""

    def __init__(
        self,
        message: str,
        *,
        module: str = "builtins",
        exception_type: str = "Exception",
        details: Mapping[str, Any] | None = None,
        mutated_inputs: Sequence[str] = (),
    ) -> None:
        if not isinstance(message, str):
            raise TypeError("target exception messages must be strings")
        if not isinstance(module, str) or _SAFE_EXCEPTION_NAME.fullmatch(module) is None:
            raise ValueError("target exception modules must be stable identifier paths")
        if (
            not isinstance(exception_type, str)
            or _SAFE_EXCEPTION_NAME.fullmatch(exception_type) is None
        ):
            raise ValueError("target exception types must be stable identifier paths")
        self.message = redact_exception_text(message)[:16_384]
        self.module = module
        self.exception_type = exception_type
        if details is not None and not isinstance(details, Mapping):
            raise TypeError("target exception details must be a mapping")
        raw_details = dict(details or {})
        self.details = normalize_exception_details(raw_details)
        if self.details != raw_details:
            raise ValueError("target exception details contain unsupported metadata")
        self.mutated_inputs = _string_tuple(mutated_inputs, label="mutation labels")
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    """Bounded identity for the runtime or executable wrapped by an adapter."""

    name: str
    version: str
    distributions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_RUNTIME_LABEL.fullmatch(self.name) is None:
            raise ValueError("runtime names must be stable labels of at most 64 characters")
        if not isinstance(self.version, str) or _SAFE_RUNTIME_LABEL.fullmatch(self.version) is None:
            raise ValueError("runtime versions must be stable labels of at most 64 characters")
        normalized = normalize_distribution_names(self.distributions)
        object.__setattr__(self, "distributions", normalized)


@dataclass(frozen=True, slots=True)
class Return:
    """A canonical adapter return with optional semantic metadata."""

    value: Any
    return_type: str | None = None
    mutated_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_return_type(self.return_type)
        object.__setattr__(
            self,
            "mutated_inputs",
            _string_tuple(self.mutated_inputs, label="mutation labels"),
        )


def _string_tuple(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{label} must be a sequence of strings")
    result = tuple(values)
    if not all(isinstance(value, str) for value in result):
        raise TypeError(f"{label} must be a sequence of strings")
    return result


def _validate_return_type(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError("return_type must be a string or None")
    if not value or len(value) > 256 or any(ord(character) < 32 for character in value):
        raise ValueError("return_type must be a non-empty, bounded text label")


def require_executable(path: str | Path) -> Path:
    """Validate one existing executable without compiling or invoking it."""

    source = Path(path)
    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise AdapterError("target_unavailable", "target executable is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or (os.name != "nt" and metadata.st_mode & 0o111 == 0):
        raise AdapterError(
            "target_unavailable",
            "target executable must resolve to a regular executable file",
        )
    return resolved


def _read_regular_file(path: Path, *, expected_parent: Path, max_bytes: int) -> bytes:
    if path.parent != expected_parent:
        raise _ProtocolFailure("protocol file is outside its call directory")
    try:
        before = path.lstat()
    except OSError:
        raise _ProtocolFailure("required protocol file is missing") from None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _ProtocolFailure("protocol files must be single-linked regular files")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _ProtocolFailure("protocol file could not be opened safely") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not os.path.samestat(before, opened)
            or opened.st_size > max_bytes
        ):
            raise _ProtocolFailure("protocol file changed before it was opened")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise _ProtocolFailure("protocol file exceeds its size limit")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not os.path.samestat(opened, after)
            or not os.path.samestat(opened, current)
            or opened.st_size != after.st_size
            or after.st_size != len(payload)
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            raise _ProtocolFailure("protocol file changed while it was read")
        return payload
    except OSError:
        raise _ProtocolFailure("protocol file could not be read safely") from None
    finally:
        os.close(descriptor)


def _exact_child(
    call_root: Path,
    raw_path: Any,
    expected_name: str,
    *,
    must_exist: bool,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise _ProtocolFailure("invalid protocol path")
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts or path.name != expected_name:
        raise _ProtocolFailure("protocol path is not the expected call-directory child")
    try:
        declared_parent = path.parent.resolve(strict=True)
    except OSError:
        raise _ProtocolFailure("protocol path parent is unavailable") from None
    if declared_parent != call_root:
        raise _ProtocolFailure("protocol path is not the expected call-directory child")
    canonical = call_root / expected_name
    if must_exist and not os.path.lexists(canonical):
        raise _ProtocolFailure("required protocol file is missing")
    if not must_exist and os.path.lexists(canonical):
        raise _ProtocolFailure("protocol output path already exists")
    return canonical


def _atomic_publish(path: Path, payload: bytes, *, max_bytes: int) -> None:
    if len(payload) > max_bytes:
        raise AdapterError("output_too_large", "canonical adapter output exceeds its size limit")
    if os.path.lexists(path):
        raise _ProtocolFailure("protocol output path already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != len(payload):
            raise _ProtocolFailure("protocol output publication was incomplete")
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise _ProtocolFailure("protocol output path appeared during publication") from None
        staged = temporary.stat()
        published = path.lstat()
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 2
            or not os.path.samestat(staged, published)
        ):
            path.unlink(missing_ok=True)
            raise _ProtocolFailure("protocol output changed during publication")
        temporary.unlink()
        final = path.lstat()
        if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
            path.unlink(missing_ok=True)
            raise _ProtocolFailure("protocol output publication was not isolated")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_payload(value: Any) -> bytes:
    def valid_keys(item: Any, seen: set[int]) -> bool:
        if isinstance(item, Mapping):
            marker = id(item)
            if marker in seen:
                return False
            seen.add(marker)
            try:
                return all(
                    isinstance(key, str) and valid_keys(nested, seen)
                    for key, nested in item.items()
                )
            finally:
                seen.remove(marker)
        if isinstance(item, (list, tuple)):
            marker = id(item)
            if marker in seen:
                return False
            seen.add(marker)
            try:
                return all(valid_keys(nested, seen) for nested in item)
            finally:
                seen.remove(marker)
        return True

    try:
        if not valid_keys(value, set()):
            raise TypeError
        encoded = json.dumps(
            value,
            allow_nan=True,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AdapterError(
            "unsupported_return",
            "adapter return is neither canonical Arrow nor portable JSON",
        ) from None
    return encoded


def _arrow_payload(value: Any) -> bytes | None:
    if isinstance(value, pa.RecordBatch):
        value = pa.Table.from_batches([value])
    if not isinstance(value, pa.Table):
        return None
    try:
        table = value.combine_chunks()
        sink = pa.BufferOutputStream()
        with ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
        return cast(bytes, sink.getvalue().to_pybytes())
    except Exception:
        raise AdapterError(
            "canonicalization_failed", "adapter Arrow return could not be canonicalized"
        ) from None


def _distribution(name: str) -> DistributionProvenance:
    try:
        version = importlib.metadata.version(name)
    except Exception:
        return DistributionProvenance(name=name, status="missing", version=None)
    if not isinstance(version, str) or _SAFE_VERSION.fullmatch(version) is None:
        return DistributionProvenance(name=name, status="unavailable", version=None)
    return DistributionProvenance(name=name, status="installed", version=version)


def _exception_payload(
    *, module: str, exception_type: str, message: str, details: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "module": module,
        "type": exception_type,
        "message": redact_exception_text(message)[:16_384],
        "details": normalize_exception_details(details),
    }


class CommandAdapter:
    """Serve one domain adapter through Parity target protocol v1."""

    def __init__(
        self,
        *,
        runtime: RuntimeInfo,
        execute: Callable[..., Any],
        inspect: Callable[[], None] | None = None,
        return_type: str | None = None,
    ) -> None:
        if not isinstance(runtime, RuntimeInfo):
            raise TypeError("runtime must be a RuntimeInfo")
        if not callable(execute):
            raise TypeError("execute must be callable")
        if inspect is not None and not callable(inspect):
            raise TypeError("inspect must be callable or None")
        _validate_return_type(return_type)
        self.runtime = runtime
        self.execute = execute
        self.inspect = inspect
        self.return_type = return_type
        self._runtime_cache: dict[tuple[str, ...], dict[str, Any]] = {}

    def _runtime_payload(self, requested: Any) -> dict[str, Any]:
        if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
            raise _ProtocolFailure("invalid endpoint distribution list")
        try:
            requested_names = normalize_distribution_names(requested)
        except (TypeError, ValueError):
            raise _ProtocolFailure("invalid endpoint distribution list") from None
        names = tuple(sorted({*_SDK_DISTRIBUTIONS, *self.runtime.distributions, *requested_names}))
        if len(names) > 69:
            raise _ProtocolFailure("too many endpoint distributions")
        if names not in self._runtime_cache:
            payload = RuntimeProvenance(
                executor="command",
                runtime_name=self.runtime.name,
                runtime_version=self.runtime.version,
                python_implementation=platform.python_implementation() or "unknown",
                python_version=platform.python_version() or "unknown",
                platform_system=platform.system() or "unknown",
                platform_machine=platform.machine() or "unknown",
                parity_version=__version__,
                distributions=tuple(_distribution(name) for name in names),
                identities=(),
            ).model_dump(mode="json")
            self._runtime_cache[names] = payload
        return self._runtime_cache[names]

    @staticmethod
    def _base_response(runtime: Mapping[str, Any], started: float) -> dict[str, Any]:
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "outcome": "returned",
            "duration_seconds": time.perf_counter() - started,
            "exception": None,
            "mutated_inputs": [],
            "return_type": None,
            "runtime": dict(runtime),
            "output": None,
        }

    @staticmethod
    def _error_response(
        runtime: Mapping[str, Any], started: float, error: AdapterError
    ) -> dict[str, Any]:
        response = CommandAdapter._base_response(runtime, started)
        response.update(
            outcome="error",
            exception=_exception_payload(
                module="parity.target_adapter",
                exception_type="AdapterError",
                message=error.safe_message,
                details={"error_codes": [error.code]},
            ),
        )
        return response

    @staticmethod
    def _unexpected_response(runtime: Mapping[str, Any], started: float) -> dict[str, Any]:
        return CommandAdapter._error_response(
            runtime,
            started,
            AdapterError("adapter_failure", "command adapter execution failed"),
        )

    @staticmethod
    def _validate_mutations(
        mutations: Sequence[str], expected_labels: tuple[str, ...]
    ) -> tuple[str, ...]:
        values = _string_tuple(mutations, label="mutation labels")
        selected = set(values)
        if len(values) != len(selected) or not selected.issubset(expected_labels):
            raise AdapterError("invalid_metadata", "adapter returned invalid mutation labels")
        ordered = tuple(label for label in expected_labels if label in selected)
        if values != ordered:
            raise AdapterError("invalid_metadata", "adapter mutation labels are out of order")
        return values

    @staticmethod
    def _inputs(
        request: Mapping[str, Any], call_root: Path
    ) -> tuple[str, tuple[tuple[str, pa.Table], ...]]:
        envelope = request.get("inputs")
        if not isinstance(envelope, Mapping) or set(envelope) != {"kind", "items"}:
            raise _ProtocolFailure("invalid input bundle")
        kind = envelope.get("kind")
        items = envelope.get("items")
        if kind not in {"single", "positional", "keyword"} or not isinstance(items, list):
            raise _ProtocolFailure("invalid input bundle")
        if not items or (kind == "single" and len(items) != 1):
            raise _ProtocolFailure("invalid input bundle cardinality")
        parsed: list[tuple[str, pa.Table]] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or set(item) != {"name", "path"}:
                raise _ProtocolFailure("invalid input item")
            name = item.get("name")
            if not isinstance(name, str):
                raise _ProtocolFailure("invalid input label")
            expected_name = f"input-{index:08d}.arrow"
            path = _exact_child(call_root, item.get("path"), expected_name, must_exist=True)
            payload = _read_regular_file(
                path,
                expected_parent=call_root,
                max_bytes=_MAX_ARROW_OUTPUT_BYTES,
            )
            try:
                table = ipc.open_file(pa.BufferReader(payload)).read_all()
            except Exception:
                raise _ProtocolFailure("canonical Arrow input could not be decoded") from None
            parsed.append((name, table))
        labels = tuple(name for name, _table in parsed)
        if len(labels) != len(set(labels)):
            raise _ProtocolFailure("input labels must be unique")
        if kind == "single" and labels != ("input",):
            raise _ProtocolFailure("invalid single input labels")
        if kind == "positional" and labels != tuple(str(index) for index in range(len(items))):
            raise _ProtocolFailure("invalid positional input labels")
        if kind == "keyword" and any(
            not name.isidentifier() or keyword.iskeyword(name) for name in labels
        ):
            raise _ProtocolFailure("invalid keyword input labels")
        return str(kind), tuple(parsed)

    def _execute_response(
        self,
        request: Mapping[str, Any],
        call_root: Path,
        runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        static_args = request.get("static_args")
        static_kwargs = request.get("static_kwargs")
        if not isinstance(static_args, list) or not isinstance(static_kwargs, dict):
            raise _ProtocolFailure("invalid static arguments")
        if not all(isinstance(name, str) for name in static_kwargs):
            raise _ProtocolFailure("static keyword names must be strings")
        output = request.get("output")
        if not isinstance(output, Mapping) or set(output) != {"arrow", "json"}:
            raise _ProtocolFailure("invalid output paths")
        output_arrow = _exact_child(
            call_root, output.get("arrow"), "output.arrow", must_exist=False
        )
        output_json = _exact_child(call_root, output.get("json"), "output.json", must_exist=False)
        kind, parsed = self._inputs(request, call_root)
        labels = tuple(name for name, _table in parsed)
        values = [table for _name, table in parsed]
        started = time.perf_counter()
        try:
            if kind == "single":
                returned = self.execute(values[0], *static_args, **static_kwargs)
            elif kind == "positional":
                returned = self.execute(*values, *static_args, **static_kwargs)
            else:
                if static_args:
                    raise AdapterError(
                        "invalid_binding",
                        "keyword inputs cannot be combined with static positional args",
                    )
                keywords = dict(parsed)
                if set(keywords).intersection(static_kwargs):
                    raise AdapterError(
                        "invalid_binding", "input names collide with static keyword arguments"
                    )
                keywords.update(static_kwargs)
                returned = self.execute(**keywords)
        except TargetRaised as error:
            try:
                mutations = self._validate_mutations(error.mutated_inputs, labels)
            except (AdapterError, TypeError, ValueError):
                return self._unexpected_response(runtime, started)
            response = self._base_response(runtime, started)
            response.update(
                outcome="raised",
                exception=_exception_payload(
                    module=error.module,
                    exception_type=error.exception_type,
                    message=error.message,
                    details=error.details,
                ),
                mutated_inputs=list(mutations),
            )
            return response
        except _ProtocolFailure:
            raise
        except AdapterError as error:
            return self._error_response(runtime, started, error)
        except BaseException:
            return self._unexpected_response(runtime, started)

        try:
            metadata = returned if isinstance(returned, Return) else Return(returned)
            mutations = self._validate_mutations(metadata.mutated_inputs, labels)
            value = metadata.value
            return_type = (
                metadata.return_type
                or self.return_type
                or f"{type(value).__module__}.{type(value).__qualname__}"
            )
            _validate_return_type(return_type)
            arrow = _arrow_payload(value)
            if arrow is not None:
                _atomic_publish(output_arrow, arrow, max_bytes=_MAX_ARROW_OUTPUT_BYTES)
                output_metadata = {"kind": "arrow"}
            else:
                payload = _json_payload(value)
                _atomic_publish(output_json, payload, max_bytes=_MAX_JSON_OUTPUT_BYTES)
                output_metadata = {"kind": "json"}
        except _ProtocolFailure:
            raise
        except AdapterError as error:
            response = self._error_response(runtime, started, error)
            response["mutated_inputs"] = list(mutations if "mutations" in locals() else ())
            response["return_type"] = locals().get("return_type")
            return response
        except BaseException:
            return self._unexpected_response(runtime, started)
        response = self._base_response(runtime, started)
        response.update(
            mutated_inputs=list(mutations),
            return_type=return_type,
            output=output_metadata,
        )
        return response

    def _run_request(self, call_root: Path) -> None:
        request_path = call_root / "request.json"
        response_path = call_root / "response.json"
        request_payload = _read_regular_file(
            request_path,
            expected_parent=call_root,
            max_bytes=_MAX_REQUEST_BYTES,
        )
        try:
            request = json.loads(request_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _ProtocolFailure("request is not valid JSON") from None
        if not isinstance(request, dict) or request.get("protocol_version") != _PROTOCOL_VERSION:
            raise _ProtocolFailure("unsupported target request")
        endpoint = request.get("endpoint")
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"kind", "record_distributions"}
            or endpoint.get("kind") != "command"
        ):
            raise _ProtocolFailure("invalid command endpoint")
        runtime = self._runtime_payload(endpoint.get("record_distributions"))
        operation = request.get("operation")
        started = time.perf_counter()
        if operation == "runtime":
            response = self._base_response(runtime, started)
        elif operation == "inspect":
            try:
                if self.inspect is not None:
                    self.inspect()
            except AdapterError as error:
                response = self._error_response(runtime, started, error)
            except BaseException:
                response = self._unexpected_response(runtime, started)
            else:
                response = self._base_response(runtime, started)
        elif operation == "execute":
            response = self._execute_response(request, call_root, runtime)
        else:
            raise _ProtocolFailure("unsupported target operation")
        encoded = json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        _atomic_publish(response_path, encoded, max_bytes=_MAX_RESPONSE_BYTES)

    def serve(self, session_root: str | Path | None = None) -> NoReturn:
        """Serve requests until stdin closes, failing silently and closed on corruption."""

        try:
            if session_root is None:
                if len(sys.argv) < 2:
                    raise _ProtocolFailure("adapter requires one session root")
                session_root = sys.argv[-1]
            root = Path(session_root)
            if not root.is_absolute():
                root = root.resolve()
            metadata = root.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
                raise _ProtocolFailure("invalid session root")
            root = root.resolve(strict=True)
            for raw_token in sys.stdin.buffer:
                try:
                    token = raw_token.rstrip(b"\r\n").decode("ascii")
                except UnicodeDecodeError:
                    raise _ProtocolFailure("invalid call token") from None
                if _CALL_TOKEN.fullmatch(token) is None:
                    raise _ProtocolFailure("invalid call token")
                call_root = root / token
                try:
                    call_metadata = call_root.lstat()
                except OSError:
                    raise _ProtocolFailure("call directory is missing") from None
                if (
                    not stat.S_ISDIR(call_metadata.st_mode)
                    or call_root.is_symlink()
                    or call_root.resolve(strict=True).parent != root
                ):
                    raise _ProtocolFailure("invalid call directory")
                self._run_request(call_root)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise SystemExit(1) from None
        raise SystemExit(0)


def serve(
    execute: Callable[..., Any],
    *,
    runtime: RuntimeInfo,
    inspect: Callable[[], None] | None = None,
    return_type: str | None = None,
    session_root: str | Path | None = None,
) -> NoReturn:
    """Convenience wrapper for constructing and serving one command adapter."""

    CommandAdapter(
        runtime=runtime,
        execute=execute,
        inspect=inspect,
        return_type=return_type,
    ).serve(session_root)


__all__ = [
    "AdapterError",
    "CommandAdapter",
    "Return",
    "RuntimeInfo",
    "TargetRaised",
    "require_executable",
    "serve",
]
