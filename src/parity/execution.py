"""Execute complete user invocations without leaking their inputs.

Arrow frames remain the canonical tabular boundary while JSON values retain their
call positions. Each implementation receives fresh adapter-specific frame copies.
The module never prints user data, subprocess output, environment variables, or
tracebacks. Isolated execution uses private files rather than a log stream.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast, get_type_hints

import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc

from parity._version import __version__
from parity.adapters import from_arrow as adapter_from_arrow
from parity.adapters import to_arrow as adapter_to_arrow
from parity.exception_semantics import (
    exception_fingerprint,
    extract_exception_details,
    is_message_fingerprint,
    normalize_exception_details,
    normalize_exception_message,
    redact_exception_text,
)
from parity.invocation import FrameSequence, Invocation, InvocationValue
from parity.models import CallableSpec, JsonValue, PandasInput, RunMetrics
from parity.provenance import (
    RuntimeProvenance,
    collect_runtime_provenance,
    diff_runtime,
    runtime_contract_failures,
)
from parity.targets import is_import_target

_TARGET_PROTOCOL_VERSION = 2
_MAX_TARGET_RESPONSE_BYTES = 1024 * 1024
_MAX_TARGET_JSON_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_TARGET_ARROW_OUTPUT_BYTES = 256 * 1024 * 1024
_PROTOCOL_READ_CHUNK_BYTES = 1024 * 1024


class ExecutionOutcome(StrEnum):
    """How an implementation invocation ended."""

    RETURNED = "returned"
    RAISED = "raised"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"


@dataclass(frozen=True, slots=True)
class ExceptionInfo:
    """Portable, deliberately traceback-free exception information."""

    module: str
    type: str
    message: str
    message_fingerprint: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        semantics = normalize_exception_message(self.message)
        object.__setattr__(self, "message", semantics.pattern)
        object.__setattr__(
            self,
            "message_fingerprint",
            self.message_fingerprint
            if is_message_fingerprint(self.message_fingerprint)
            else semantics.fingerprint,
        )
        object.__setattr__(self, "details", normalize_exception_details(self.details))

    @classmethod
    def from_exception(cls, error: BaseException) -> ExceptionInfo:
        semantics = normalize_exception_message(str(error))
        return cls(
            module=type(error).__module__,
            type=type(error).__qualname__,
            message=semantics.pattern,
            message_fingerprint=semantics.fingerprint,
            details=extract_exception_details(error),
        )

    @property
    def fingerprint(self) -> str:
        """Opaque semantic identity used by comparison and finding signatures."""

        return exception_fingerprint(
            self.module,
            self.type,
            self.message_fingerprint or normalize_exception_message(self.message).fingerprint,
            self.details,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "type": self.type,
            "message": self.message,
            "message_fingerprint": self.message_fingerprint,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExceptionInfo:
        return cls(
            module=str(value.get("module", "builtins")),
            type=str(value.get("type", "Exception")),
            message=str(value.get("message", "")),
            message_fingerprint=(
                str(value["message_fingerprint"])
                if is_message_fingerprint(value.get("message_fingerprint"))
                else None
            ),
            details=normalize_exception_details(value.get("details")),
        )


@dataclass(slots=True)
class Observation:
    """The complete, serializable observation of one implementation call.

    ``table`` and ``value`` are kept out of :meth:`to_metadata` so diagnostic
    output cannot accidentally log input data.  The worker protocol writes
    returned values to private files and only serializes this metadata as JSON.
    """

    outcome: ExecutionOutcome
    metrics: RunMetrics
    table: pa.Table | None = None
    value: JsonValue = None
    has_value: bool = False
    exception: ExceptionInfo | None = None
    mutated_inputs: tuple[str, ...] = ()
    return_type: str | None = None
    runtime: RuntimeProvenance | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is ExecutionOutcome.RETURNED

    def to_metadata(self) -> dict[str, Any]:
        """Return JSON metadata with all user return data omitted."""

        return {
            "outcome": self.outcome.value,
            "metrics": self.metrics.model_dump(mode="json"),
            "has_table": self.table is not None,
            "has_value": self.has_value,
            "exception": self.exception.to_dict() if self.exception else None,
            "mutated_inputs": list(self.mutated_inputs),
            "return_type": self.return_type,
            "runtime": self.runtime.model_dump(mode="json") if self.runtime else None,
        }


class ExecutionError(RuntimeError):
    """Raised for invalid execution configuration, never for user exceptions."""


_PROCESS_CONTEXT_LOCK = threading.RLock()


def _normalize_inputs(inputs: Invocation) -> Invocation:
    """Validate the complete public invocation contract."""

    if not isinstance(inputs, Invocation):
        raise ExecutionError("input must be a parity.Invocation")
    return inputs.copy()


def _materialize_value(
    value: InvocationValue,
    adapter: str,
    *,
    pandas_input: PandasInput,
) -> Any:
    if isinstance(value, pa.Table):
        return _fresh_argument(value, adapter, pandas_input=pandas_input)
    if isinstance(value, FrameSequence):
        items = [
            _fresh_argument(table, adapter, pandas_input=pandas_input) for table in value.items
        ]
        return items if value.container == "list" else tuple(items)
    # JSON round-tripping both detaches mutable containers and preserves the
    # strict cross-process value contract used by command targets.
    return json.loads(json.dumps(value, allow_nan=True))


def _materialize_inputs(
    invocation: Invocation,
    adapter: str,
    *,
    pandas_input: PandasInput,
) -> tuple[tuple[Any, ...], dict[str, Any], dict[str, str | None]]:
    args = tuple(
        _materialize_value(value, adapter, pandas_input=pandas_input) for value in invocation.args
    )
    kwargs = {
        name: _materialize_value(value, adapter, pandas_input=pandas_input)
        for name, value in invocation.kwargs.items()
    }
    fingerprints = _invocation_fingerprints(invocation, args, kwargs, adapter)
    return args, kwargs, fingerprints


def _invoke_with_inputs(
    function: Callable[..., Any],
    arguments: tuple[Any, ...],
    keyword_arguments: Mapping[str, Any],
) -> Any:
    return function(*arguments, **dict(keyword_arguments))


def _json_fingerprint(value: Any) -> str | None:
    try:
        payload = json.dumps(value, allow_nan=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _argument_fingerprint(canonical: InvocationValue, value: Any, adapter: str) -> str | None:
    if isinstance(canonical, pa.Table):
        return _fingerprint(value, adapter)
    if isinstance(canonical, FrameSequence):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return None
        parts = [_fingerprint(item, adapter) for item in value]
        if any(part is None for part in parts):
            return None
        return hashlib.sha256("\0".join(cast(list[str], parts)).encode("ascii")).hexdigest()
    return _json_fingerprint(value)


def _invocation_fingerprints(
    invocation: Invocation,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    adapter: str,
) -> dict[str, str | None]:
    fingerprints = {
        f"args/{index}": _argument_fingerprint(canonical, args[index], adapter)
        for index, canonical in enumerate(invocation.args)
    }
    fingerprints.update(
        {
            f"kwargs/{name}": _argument_fingerprint(canonical, kwargs[name], adapter)
            for name, canonical in invocation.kwargs.items()
        }
    )
    return fingerprints


def _mutated_labels(
    invocation: Invocation,
    arguments: tuple[Any, ...],
    keyword_arguments: Mapping[str, Any],
    before: Mapping[str, str | None],
    adapter: str,
) -> tuple[str, ...]:
    after = _invocation_fingerprints(invocation, arguments, keyword_arguments, adapter)
    return tuple(label for label in before if before[label] != after[label])


def redact_text(text: str) -> str:
    """Remove common path and secret-assignment forms from diagnostic text."""

    return redact_exception_text(text)


def import_callable(target: str) -> Callable[..., Any]:
    """Import an explicit ``module:attribute`` target without using ``eval``."""

    if not is_import_target(target):
        raise ExecutionError("callable target must be in module.path:function.path form")
    module_name, attribute_path = target.split(":", 1)
    try:
        value: Any = importlib.import_module(module_name)
    except Exception as error:
        raise ExecutionError(
            f"could not import module {module_name!r}: {type(error).__name__}: {redact_text(str(error))}"
        ) from error
    try:
        for part in attribute_path.split("."):
            value = getattr(value, part)
    except AttributeError as error:
        raise ExecutionError(f"callable target {target!r} does not exist") from error
    if not callable(value):
        raise ExecutionError(f"target {target!r} is not callable")
    return cast(Callable[..., Any], value)


def _write_arrow(table: pa.Table, path: Path) -> None:
    with path.open("wb") as stream, ipc.new_file(stream, table.schema) as writer:
        writer.write_table(table)


def _read_arrow(path: Path) -> pa.Table:
    with path.open("rb") as stream:
        return ipc.open_file(stream).read_all()


def _read_protocol_file(
    call_root: Path,
    path: Path,
    *,
    expected_name: str,
    max_bytes: int,
) -> bytes:
    """Read one stable regular protocol file without following redirects.

    Command targets are trusted code, but their protocol output is still parsed
    defensively.  Open-by-descriptor plus before/after identity checks reject
    symlinks, hard links, replacements and concurrent mutation.  Reading at most
    ``max_bytes + 1`` makes the size limit authoritative even if a broken target
    appends after publishing the path.
    """

    if path.parent != call_root or path.name != expected_name:
        raise ValueError("target protocol path is not an immediate call-directory child")
    if max_bytes < 0:  # pragma: no cover - internal constant contract
        raise ValueError("target protocol size limit must be non-negative")
    try:
        before = path.lstat()
    except OSError as error:
        raise ValueError("target protocol file is missing") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("target protocol file must be a single-linked regular file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("target protocol file could not be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not os.path.samestat(before, opened)
        ):
            raise ValueError("target protocol file changed before it was opened")
        if opened.st_size > max_bytes:
            raise ValueError("target protocol file exceeds its size limit")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(_PROTOCOL_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ValueError("target protocol file exceeds its size limit")

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
            raise ValueError("target protocol file changed while it was read")
        return payload
    except OSError as error:
        raise ValueError("target protocol file could not be read safely") from error
    finally:
        os.close(descriptor)


def _read_target_arrow(call_root: Path, path: Path) -> pa.Table:
    payload = _read_protocol_file(
        call_root,
        path,
        expected_name="output.arrow",
        max_bytes=_MAX_TARGET_ARROW_OUTPUT_BYTES,
    )
    return ipc.open_file(pa.BufferReader(payload)).read_all()


def _response_publication_finished(path: Path) -> bool:
    """Distinguish a completed response from the SDK's atomic link window.

    The command-adapter SDK publishes without overwriting by linking a complete
    temporary inode to ``response.json`` and then unlinking the temporary name.
    During that short interval the response is complete but deliberately has
    two links, so the strict protocol reader must not inspect it yet. Other
    non-regular or multiply-linked responses remain invalid and are passed to
    the reader for fail-closed validation.
    """

    try:
        metadata = path.lstat()
    except OSError:
        return False
    return not (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 2)


def _arrow_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return cast(bytes, sink.getvalue().to_pybytes())


def _fingerprint(value: Any, adapter: str) -> str | None:
    try:
        table = _argument_to_arrow(value, adapter, preserve_index=True)
        return hashlib.sha256(_arrow_bytes(table)).hexdigest()
    except Exception:
        # If a callable mutates its input into an object no longer convertible to
        # Arrow, treating it as mutation is safer than suppressing the signal.
        return None


def _annotation_adapter(function: Callable[..., Any]) -> str | None:
    try:
        annotations = get_type_hints(function)
    except Exception:
        annotations = getattr(function, "__annotations__", {})
    for name, annotation in annotations.items():
        if name == "return":
            continue
        module = getattr(annotation, "__module__", "")
        qualified = f"{module}.{getattr(annotation, '__qualname__', annotation)}".lower()
        if "pandas" in qualified:
            return "pandas"
        if "polars" in qualified:
            return "polars"
        if "pyarrow" in qualified or "arrow" in qualified:
            return "arrow"
        break
    return None


def _resolve_adapter(requested: str, function: Callable[..., Any]) -> str:
    if requested != "auto":
        return requested
    # Pandas is the conservative default for unannotated Python dataframe code;
    # annotations make auto mode deterministic for Polars and Arrow callables.
    return _annotation_adapter(function) or "pandas"


def _fresh_argument(table: pa.Table, adapter: str, *, pandas_input: PandasInput = "arrow") -> Any:
    # Round-tripping creates a fresh Arrow object and prevents implementations
    # from sharing Python wrapper state even when they both use the Arrow adapter.
    try:
        fresh = ipc.open_stream(pa.py_buffer(_arrow_bytes(table))).read_all()
        if adapter == "pandas" and pandas_input == "native":
            return fresh.to_pandas()
        return adapter_from_arrow(fresh, adapter)
    except Exception:
        # Adapter exceptions may include values or native-library details. The
        # caller only needs to know that Parity could not construct the input;
        # this is an infrastructure error, never a matching user exception.
        raise ExecutionError(f"input could not be materialized for adapter: {adapter}") from None


def _argument_to_arrow(value: Any, adapter: str, *, preserve_index: bool = False) -> pa.Table:
    if not preserve_index:
        return adapter_to_arrow(value, adapter)
    if adapter == "arrow":
        if isinstance(value, pa.Table):
            return value
        if isinstance(value, pa.RecordBatch):
            return pa.Table.from_batches([value])
    elif adapter == "pandas":
        import pandas as pd

        if isinstance(value, pd.Series):
            value = value.to_frame()
        if isinstance(value, pd.DataFrame):
            # Index changes participate in mutation detection even though index
            # labels are not part of the cross-engine output contract.
            table = pa.Table.from_pandas(value, preserve_index=True).combine_chunks()
            for index in range(value.shape[1]):
                field = table.schema.field(index)
                source = value.iloc[:, index]
                floating_numpy_dtype = isinstance(source.dtype, np.dtype) and np.issubdtype(
                    source.dtype, np.floating
                )
                if not floating_numpy_dtype:
                    continue
                values = [
                    None if item is pd.NA or item is pd.NaT else item for item in source.tolist()
                ]
                table = table.set_column(
                    index,
                    field,
                    pa.array(values, type=field.type, from_pandas=False),
                )
            return table
    elif adapter == "polars":
        import polars as pl

        if isinstance(value, pl.LazyFrame):
            value = value.collect()
        if isinstance(value, pl.Series):
            value = value.to_frame()
        if isinstance(value, pl.DataFrame):
            converted = value.to_arrow()
            if isinstance(converted, pa.Table):
                return converted
    raise TypeError(f"expected a {adapter} dataframe return, got {type(value).__name__}")


def _return_to_arrow(value: Any) -> pa.Table | None:
    if isinstance(value, (pa.Table, pa.RecordBatch)):
        return _argument_to_arrow(value, "arrow")
    try:
        import pandas as pd

        if isinstance(value, (pd.DataFrame, pd.Series)):
            return _argument_to_arrow(value, "pandas")
    except ImportError:
        pass
    try:
        import polars as pl

        if isinstance(value, (pl.DataFrame, pl.LazyFrame, pl.Series)):
            return _argument_to_arrow(value, "polars")
    except ImportError:
        pass
    return None


def _json_value(value: Any) -> tuple[bool, JsonValue]:
    # Round-trip rather than using ``default=str``: object reprs can expose data,
    # paths, or credentials and have no stable cross-environment semantics.
    def has_only_string_mapping_keys(item: Any) -> bool:
        if isinstance(item, Mapping):
            return all(
                isinstance(key, str) and has_only_string_mapping_keys(nested)
                for key, nested in item.items()
            )
        if isinstance(item, (list, tuple)):
            return all(has_only_string_mapping_keys(nested) for nested in item)
        return True

    # JSON silently coerces integer/bool/null object keys to strings. Reject
    # them recursively so the comparison boundary cannot erase key-type
    # differences and produce a false pass.
    try:
        valid_mapping_keys = has_only_string_mapping_keys(value)
    except Exception:
        # Cycles and hostile container implementations are not a portable JSON
        # value. Preserve the normal unsupported-return failure classification.
        return False, None
    if not valid_mapping_keys:
        return False, None
    try:
        encoded = json.dumps(value, allow_nan=True)
        decoded: JsonValue = json.loads(encoded)
    except (TypeError, ValueError, OverflowError):
        return False, None
    return True, decoded


class _MemorySampler:
    def __init__(self, pid: int | None = None) -> None:
        self.own_process = pid is None or pid == os.getpid()
        self.pid = pid or os.getpid()
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        try:
            import psutil

            try:
                process = psutil.Process(self.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return
            while not self._stop.is_set():
                total = 0
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    processes = [process, *process.children(recursive=True)]
                    for child in processes:
                        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                            total += child.memory_info().rss
                self.peak = max(self.peak, total)
                self._stop.wait(0.005)
        except ImportError:
            return

    def __enter__(self) -> _MemorySampler:
        if self.own_process:
            self.peak = max(self.peak, _resource_peak_rss())
        try:
            import psutil

            process = psutil.Process(self.pid)
            self.peak = process.memory_info().rss
        except Exception:
            # A very short worker can exit between Popen and the first sample.
            pass
        self._thread = threading.Thread(target=self._sample, name="parity-rss", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            # Sampling waits at most 5 ms between iterations.  A full join is
            # important here: leaving a daemon sampler in psutil/native process
            # inspection during interpreter teardown can make Arrow/Polars
            # shutdown nondeterministic after a large isolated campaign.
            self._thread.join()
        if self.own_process:
            self.peak = max(self.peak, _resource_peak_rss())


def _resource_peak_rss() -> int:
    """Return process peak RSS via the OS when process sampling is unavailable."""

    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux and the BSDs report KiB; macOS reports bytes.
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, OSError, ValueError):
        return 0


@contextlib.contextmanager
def _process_context(spec: CallableSpec) -> Iterator[None]:
    # cwd and os.environ are process-global. Serializing the temporary context
    # prevents concurrent live API calls from observing one another's settings.
    with _PROCESS_CONTEXT_LOCK:
        old_directory = Path.cwd()
        original: dict[str, str | None] = {key: os.environ.get(key) for key in spec.environment}
        try:
            if spec.workdir is not None:
                os.chdir(spec.workdir)
            os.environ.update(spec.environment)
            yield
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.chdir(old_directory)


def _runtime_drift_observation(
    expected: RuntimeProvenance | None,
    actual: RuntimeProvenance,
    *,
    started: float,
    peak_rss_bytes: int | None,
) -> Observation | None:
    if expected is None or not (differences := diff_runtime(expected, actual)):
        return None
    return Observation(
        outcome=ExecutionOutcome.ERROR,
        exception=ExceptionInfo(
            module="parity.execution",
            type="RuntimeDriftError",
            message="runtime provenance differs for: " + ", ".join(differences),
        ),
        metrics=RunMetrics(
            duration_seconds=time.perf_counter() - started,
            peak_rss_bytes=peak_rss_bytes,
        ),
        runtime=actual,
    )


def _runtime_contract_observation(
    spec: CallableSpec,
    actual: RuntimeProvenance,
    *,
    metrics: RunMetrics,
    expected_parity_version: str | None = None,
) -> Observation | None:
    failures = runtime_contract_failures(
        actual,
        expected_parity_version=(
            expected_parity_version or __version__ if actual.executor == "parity-python" else None
        ),
        required_distributions=spec.required_distributions,
    )
    if not failures:
        return None
    return Observation(
        outcome=ExecutionOutcome.ERROR,
        exception=ExceptionInfo(
            module="parity.execution",
            type="RuntimeContractError",
            message="worker runtime requirements not satisfied: " + ", ".join(failures),
        ),
        metrics=metrics,
        runtime=actual,
    )


def _preflight_failure_observation(
    observation: Observation,
    *,
    phase: Literal["transport", "endpoint"],
    runtime: RuntimeProvenance | None = None,
) -> Observation:
    """Attach a stable, data-safe reason code to one readiness failure."""

    label = "TargetTransportError" if phase == "transport" else "TargetEndpointError"
    return Observation(
        outcome=observation.outcome,
        exception=ExceptionInfo(
            module="parity.execution",
            type=label,
            message=f"target {phase} preflight failed",
        ),
        metrics=observation.metrics,
        runtime=observation.runtime or runtime,
    )


def execute_current(
    spec: CallableSpec,
    invocation: Invocation,
    *,
    expected_runtime: RuntimeProvenance | None = None,
    expected_parity_version: str | None = None,
) -> Observation:
    """Execute a callable in this interpreter and capture its observation.

    This mode is fast and useful for live Python APIs.  It does not attempt to
    enforce a timeout because a Python thread cannot be terminated safely; use
    :func:`execute_isolated` whenever cancellation or environment isolation is
    required.
    """

    inputs = _normalize_inputs(invocation)
    if spec.command is not None:
        raise ExecutionError("command endpoints require isolated protocol execution")
    if spec.python is not None and Path(spec.python).resolve() != Path(sys.executable).resolve():
        raise ExecutionError("a different Python executable requires isolated execution")
    started = time.perf_counter()
    runtime: RuntimeProvenance | None = None
    return_type: str | None = None
    with _MemorySampler() as memory:
        try:
            with _process_context(spec):
                runtime = collect_runtime_provenance(spec.provenance_distributions)
                if contract_failure := _runtime_contract_observation(
                    spec,
                    runtime,
                    metrics=RunMetrics(
                        duration_seconds=time.perf_counter() - started,
                        peak_rss_bytes=memory.peak or None,
                    ),
                    expected_parity_version=expected_parity_version,
                ):
                    return contract_failure
                if drift := _runtime_drift_observation(
                    expected_runtime,
                    runtime,
                    started=started,
                    peak_rss_bytes=memory.peak or None,
                ):
                    return drift
                if spec.target is None:  # pragma: no cover - CallableSpec invariant
                    raise ExecutionError("Python target is missing")
                function = import_callable(spec.target)
                canonicalizer = (
                    import_callable(spec.canonicalizer) if spec.canonicalizer is not None else None
                )
                adapter = _resolve_adapter(spec.adapter, function)
                arguments, keyword_arguments, before = _materialize_inputs(
                    inputs, adapter, pandas_input=spec.pandas_input
                )
                try:
                    returned = _invoke_with_inputs(
                        function,
                        arguments,
                        keyword_arguments,
                    )
                except BaseException as error:  # user code is an observation boundary
                    mutated_inputs = _mutated_labels(
                        inputs, arguments, keyword_arguments, before, adapter
                    )
                    return Observation(
                        outcome=ExecutionOutcome.RAISED,
                        exception=ExceptionInfo.from_exception(error),
                        mutated_inputs=mutated_inputs,
                        metrics=RunMetrics(
                            duration_seconds=time.perf_counter() - started,
                            peak_rss_bytes=memory.peak or None,
                        ),
                        runtime=runtime,
                    )
                mutated_inputs = _mutated_labels(
                    inputs, arguments, keyword_arguments, before, adapter
                )
                return_type = f"{type(returned).__module__}.{type(returned).__qualname__}"
                if canonicalizer is not None:
                    try:
                        returned = canonicalizer(returned)
                    except BaseException:
                        canonicalization_error = ExecutionError(
                            "target output canonicalizer failed"
                        )
                        return Observation(
                            outcome=ExecutionOutcome.ERROR,
                            exception=ExceptionInfo.from_exception(canonicalization_error),
                            mutated_inputs=mutated_inputs,
                            return_type=return_type,
                            metrics=RunMetrics(
                                duration_seconds=time.perf_counter() - started,
                                peak_rss_bytes=memory.peak or None,
                            ),
                            runtime=runtime,
                        )
                try:
                    table = _return_to_arrow(returned)
                except Exception:
                    # Returned tabular objects can fail while crossing the
                    # canonical Arrow boundary (unsupported dtype, malformed
                    # extension array, etc.). Report a data-safe Parity error;
                    # two matching conversion failures cannot establish parity.
                    conversion_error = ExecutionError(
                        f"return type {return_type} could not be canonicalized"
                    )
                    return Observation(
                        outcome=ExecutionOutcome.ERROR,
                        exception=ExceptionInfo.from_exception(conversion_error),
                        mutated_inputs=mutated_inputs,
                        return_type=return_type,
                        metrics=RunMetrics(
                            duration_seconds=time.perf_counter() - started,
                            peak_rss_bytes=memory.peak or None,
                        ),
                        runtime=runtime,
                    )
                if table is not None:
                    return Observation(
                        outcome=ExecutionOutcome.RETURNED,
                        table=table,
                        mutated_inputs=mutated_inputs,
                        return_type=return_type,
                        metrics=RunMetrics(
                            duration_seconds=time.perf_counter() - started,
                            peak_rss_bytes=memory.peak or None,
                        ),
                        runtime=runtime,
                    )
                serializable, value = _json_value(returned)
                if not serializable:
                    return_error = TypeError(
                        f"return type {return_type} is neither tabular nor JSON-serializable"
                    )
                    return Observation(
                        outcome=ExecutionOutcome.ERROR,
                        exception=ExceptionInfo.from_exception(return_error),
                        mutated_inputs=mutated_inputs,
                        return_type=return_type,
                        metrics=RunMetrics(
                            duration_seconds=time.perf_counter() - started,
                            peak_rss_bytes=memory.peak or None,
                        ),
                        runtime=runtime,
                    )
                return Observation(
                    outcome=ExecutionOutcome.RETURNED,
                    value=value,
                    has_value=True,
                    mutated_inputs=mutated_inputs,
                    return_type=return_type,
                    metrics=RunMetrics(
                        duration_seconds=time.perf_counter() - started,
                        peak_rss_bytes=memory.peak or None,
                    ),
                    runtime=runtime,
                )
        except BaseException:
            boundary_error = ExecutionError("execution boundary failed before comparison")
            return Observation(
                outcome=ExecutionOutcome.ERROR,
                exception=ExceptionInfo.from_exception(boundary_error),
                return_type=return_type,
                metrics=RunMetrics(
                    duration_seconds=time.perf_counter() - started,
                    peak_rss_bytes=memory.peak or None,
                ),
                runtime=runtime,
            )


def execute_callable_current(
    function: Callable[..., Any],
    invocation: Invocation,
    *,
    adapter: str = "auto",
    pandas_input: PandasInput = "arrow",
    record_distributions: Sequence[str] = (),
) -> Observation:
    """Execute a live, potentially non-importable callable in this interpreter.

    This is the execution path used by :func:`parity.verify`.  It has the same
    adaptation, mutation detection, metric, and serialization semantics as a
    configured callable but intentionally cannot offer process isolation.
    """

    if not callable(function):
        raise TypeError("function must be callable")
    if adapter not in {"auto", "pandas", "polars", "arrow"}:
        raise ValueError(f"unsupported adapter: {adapter}")
    if pandas_input not in {"arrow", "native"}:
        raise ValueError(f"unsupported pandas input: {pandas_input}")
    inputs = _normalize_inputs(invocation)
    started = time.perf_counter()
    runtime: RuntimeProvenance | None = None
    return_type: str | None = None
    with _MemorySampler() as memory:
        try:
            runtime = collect_runtime_provenance(record_distributions)
            resolved = _resolve_adapter(adapter, function)
            arguments, keyword_arguments, before = _materialize_inputs(
                inputs, resolved, pandas_input=pandas_input
            )
            try:
                returned = _invoke_with_inputs(
                    function,
                    arguments,
                    keyword_arguments,
                )
            except BaseException as error:
                mutated_inputs = _mutated_labels(
                    inputs, arguments, keyword_arguments, before, resolved
                )
                return Observation(
                    outcome=ExecutionOutcome.RAISED,
                    exception=ExceptionInfo.from_exception(error),
                    mutated_inputs=mutated_inputs,
                    metrics=RunMetrics(
                        duration_seconds=time.perf_counter() - started,
                        peak_rss_bytes=memory.peak or None,
                    ),
                    runtime=runtime,
                )
            mutated_inputs = _mutated_labels(inputs, arguments, keyword_arguments, before, resolved)
            return_type = f"{type(returned).__module__}.{type(returned).__qualname__}"
            try:
                table = _return_to_arrow(returned)
            except Exception:
                conversion_error = ExecutionError(
                    f"return type {return_type} could not be canonicalized"
                )
                return Observation(
                    outcome=ExecutionOutcome.ERROR,
                    exception=ExceptionInfo.from_exception(conversion_error),
                    mutated_inputs=mutated_inputs,
                    return_type=return_type,
                    metrics=RunMetrics(
                        duration_seconds=time.perf_counter() - started,
                        peak_rss_bytes=memory.peak or None,
                    ),
                    runtime=runtime,
                )
            if table is not None:
                return Observation(
                    outcome=ExecutionOutcome.RETURNED,
                    table=table,
                    mutated_inputs=mutated_inputs,
                    return_type=return_type,
                    metrics=RunMetrics(
                        duration_seconds=time.perf_counter() - started,
                        peak_rss_bytes=memory.peak or None,
                    ),
                    runtime=runtime,
                )
            serializable, value = _json_value(returned)
            if not serializable:
                return_error = TypeError(
                    f"return type {return_type} is neither tabular nor JSON-serializable"
                )
                return Observation(
                    outcome=ExecutionOutcome.ERROR,
                    exception=ExceptionInfo.from_exception(return_error),
                    mutated_inputs=mutated_inputs,
                    return_type=return_type,
                    metrics=RunMetrics(
                        duration_seconds=time.perf_counter() - started,
                        peak_rss_bytes=memory.peak or None,
                    ),
                    runtime=runtime,
                )
            return Observation(
                outcome=ExecutionOutcome.RETURNED,
                value=value,
                has_value=True,
                mutated_inputs=mutated_inputs,
                return_type=return_type,
                metrics=RunMetrics(
                    duration_seconds=time.perf_counter() - started,
                    peak_rss_bytes=memory.peak or None,
                ),
                runtime=runtime,
            )
        except BaseException:
            boundary_error = ExecutionError("execution boundary failed before comparison")
            return Observation(
                outcome=ExecutionOutcome.ERROR,
                exception=ExceptionInfo.from_exception(boundary_error),
                return_type=return_type,
                metrics=RunMetrics(
                    duration_seconds=time.perf_counter() - started,
                    peak_rss_bytes=memory.peak or None,
                ),
                runtime=runtime,
            )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate an isolated worker and its descendants, then reap it."""

    if os.name == "posix":
        # Every worker starts a new session, so the process group includes all
        # descendants created by user code and never includes the Parity parent.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        # The group may still contain descendants even after its leader was
        # reaped; send the hard stop to the group rather than testing the leader.
        time.sleep(0.02)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        return
    if process.poll() is not None:
        return
    try:
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.terminate()
        with contextlib.suppress(psutil.NoSuchProcess):
            parent.terminate()
        _, alive = psutil.wait_procs([*children, parent], timeout=0.5)
        for remaining in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                remaining.kill()
    except (ImportError, OSError):
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        if process.poll() is None:
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _isolated_environment(spec: CallableSpec) -> dict[str, str]:
    """Build an isolated target environment without controller import leakage."""

    environment = os.environ.copy()
    # The controller's source or site-packages path can silently replace the
    # target's dependency graph. Project import comes from the explicit workdir;
    # a target that genuinely needs another root must declare PYTHONPATH itself.
    environment.pop("PYTHONPATH", None)
    if spec.native_threads is not None:
        limit = str(spec.native_threads)
        for name in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
            "BLIS_NUM_THREADS",
        ):
            # An endpoint-specific environment value is more explicit than the
            # convenience limit and remains authoritative.
            environment[name] = limit
    environment.update(spec.environment)
    return environment


def _write_invocation(invocation: Invocation, root: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Persist Arrow leaves and return the versioned invocation envelope."""

    counter = 0

    def encode(value: InvocationValue) -> dict[str, Any]:
        nonlocal counter
        if isinstance(value, pa.Table):
            path = root / f"input-{counter:08d}.arrow"
            counter += 1
            _write_arrow(value, path)
            return {"kind": "arrow", "path": str(path)}
        if isinstance(value, FrameSequence):
            return {
                "kind": "frames",
                "container": value.container,
                "items": [encode(table) for table in value.items],
            }
        return {"kind": "json", "value": value}

    envelope = {
        "args": [encode(value) for value in invocation.args],
        "kwargs": {name: encode(value) for name, value in invocation.kwargs.items()},
    }
    labels = tuple(f"args/{index}" for index in range(len(invocation.args)))
    labels += tuple(f"kwargs/{name}" for name in invocation.kwargs)
    return envelope, labels


def execute_isolated(
    spec: CallableSpec,
    invocation: Invocation,
    *,
    timeout_seconds: float = 30.0,
    expected_runtime: RuntimeProvenance | None = None,
) -> Observation:
    """Execute once in a fresh target-protocol process."""

    with IsolatedExecutionSession(
        spec,
        timeout_seconds=timeout_seconds,
        expected_runtime=expected_runtime,
    ) as session:
        return session.execute(invocation)


def _observation_from_target(
    response: Mapping[str, Any],
    call_root: Path,
    output_arrow: Path,
    output_json: Path,
    *,
    expected_input_labels: tuple[str, ...],
    allow_outputless_success: bool,
    expected_executor: Literal["portable-python", "command"],
) -> Observation:
    """Validate one dependency-light target-protocol response."""

    expected_fields = {
        "duration_seconds",
        "exception",
        "mutated_inputs",
        "outcome",
        "output",
        "protocol_version",
        "return_type",
        "runtime",
    }
    if set(response) != expected_fields:
        raise ValueError("invalid target response fields")
    if response.get("protocol_version") != _TARGET_PROTOCOL_VERSION:
        raise ValueError("unsupported target protocol")
    raw_outcome = response.get("outcome")
    if raw_outcome not in {"returned", "raised", "error"}:
        raise ValueError("invalid target outcome")
    outcome = ExecutionOutcome(str(raw_outcome))
    duration = response.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not np.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("invalid target duration")
    runtime = RuntimeProvenance.model_validate(response.get("runtime"))
    if runtime.executor != expected_executor:
        raise ValueError("target reported the wrong executor kind")

    mutated_inputs_raw = response.get("mutated_inputs")
    if not isinstance(mutated_inputs_raw, list) or not all(
        isinstance(label, str) for label in mutated_inputs_raw
    ):
        raise ValueError("invalid target mutation labels")
    mutated_inputs = tuple(mutated_inputs_raw)
    mutated_label_set = set(mutated_inputs)
    if len(mutated_inputs) != len(mutated_label_set):
        raise ValueError("duplicate target mutation labels")
    if not mutated_label_set.issubset(expected_input_labels):
        raise ValueError("unknown target mutation label")
    if mutated_inputs != tuple(
        label for label in expected_input_labels if label in mutated_label_set
    ):
        raise ValueError("target mutation labels are out of order")

    exception_raw = response.get("exception")
    if exception_raw is not None:
        if not isinstance(exception_raw, Mapping) or set(exception_raw) != {
            "module",
            "type",
            "message",
            "details",
        }:
            raise ValueError("invalid target exception metadata")
        if not all(
            isinstance(exception_raw.get(key), str) for key in ("module", "type", "message")
        ):
            raise ValueError("invalid target exception fields")
        details_raw = exception_raw.get("details")
        if not isinstance(details_raw, Mapping):
            raise ValueError("invalid target exception details")
        if normalize_exception_details(details_raw) != dict(details_raw):
            raise ValueError("unsafe target exception details")

    output_raw = response.get("output")
    output_kind: str | None
    if output_raw is None:
        output_kind = None
    elif isinstance(output_raw, Mapping) and set(output_raw) == {"kind"}:
        output_kind = output_raw.get("kind")
        if output_kind not in {"arrow", "json"}:
            raise ValueError("invalid target output kind")
    else:
        raise ValueError("invalid target output metadata")

    return_type_raw = response.get("return_type")
    if return_type_raw is not None and not isinstance(return_type_raw, str):
        raise ValueError("invalid target return type")
    if outcome is ExecutionOutcome.RETURNED:
        if exception_raw is not None:
            raise ValueError("returned target response cannot contain an exception")
        if output_kind is None and not allow_outputless_success:
            raise ValueError("target execution returned without an output")
    else:
        if exception_raw is None:
            raise ValueError("failed target response requires an exception")
        if output_kind is not None:
            raise ValueError("failed target response cannot contain an output")

    observation = Observation(
        outcome=outcome,
        metrics=RunMetrics(duration_seconds=float(duration)),
        exception=(
            ExceptionInfo(
                module=str(exception_raw["module"]),
                type=str(exception_raw["type"]),
                message=str(exception_raw["message"]),
                details=cast(Mapping[str, Any], exception_raw["details"]),
            )
            if exception_raw is not None
            else None
        ),
        mutated_inputs=mutated_inputs,
        return_type=return_type_raw,
        runtime=runtime,
    )
    if output_kind == "arrow":
        observation.table = _read_target_arrow(call_root, output_arrow)
    elif output_kind == "json":
        payload = _read_protocol_file(
            call_root,
            output_json,
            expected_name="output.json",
            max_bytes=_MAX_TARGET_JSON_OUTPUT_BYTES,
        )
        observation.value = json.loads(payload.decode("utf-8"))
        observation.has_value = True
    return observation


class IsolatedExecutionSession:
    """Execute repeated calls in one isolated worker process.

    A session removes per-example interpreter startup while retaining a process
    boundary from the orchestrator.  Each session owns exactly one callable
    specification, serializes calls, creates fresh adapter arguments from new
    Arrow files for every call, and enforces a wall-clock timeout per call.

    Python module globals, imported-module caches, threads, and other worker
    process state intentionally persist between successful calls.  Reference and
    candidate implementations therefore need distinct sessions.  After a worker
    timeout, crash, or invalid response, the session is terminated and fails
    closed: it never silently restarts with reset state.  Use the class as a
    context manager, or call :meth:`close` explicitly, to terminate the worker
    and all descendants and remove protocol files.
    """

    def __init__(
        self,
        spec: CallableSpec,
        *,
        timeout_seconds: float = 30.0,
        expected_runtime: RuntimeProvenance | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.spec = spec
        self.timeout_seconds = timeout_seconds
        self.expected_runtime = expected_runtime
        self._temporary = tempfile.TemporaryDirectory(prefix="parity-session-")
        self._root = Path(self._temporary.name)
        self._process: subprocess.Popen[bytes] | None = None
        self._counter = 0
        self._closed = False
        self._broken = False
        self._runtime_validated = False
        self._validated_runtime: RuntimeProvenance | None = None
        self._lock = threading.RLock()

    @property
    def closed(self) -> bool:
        """Whether the session has been explicitly closed or failed closed."""

        return self._closed or self._broken

    def __enter__(self) -> IsolatedExecutionSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _start(self) -> subprocess.Popen[bytes]:
        if self._process is not None:
            return self._process
        if self.spec.command is not None:
            command = [*self.spec.command, str(self._root)]
        else:
            worker = Path(__file__).with_name("portable_worker.py").resolve()
            command = [str(self.spec.python or sys.executable), str(worker), str(self._root)]
        self._process = subprocess.Popen(
            command,
            cwd=self.spec.workdir,
            env=_isolated_environment(self.spec),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        return self._process

    def _stop_worker(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        _terminate_process(process)

    def _fail_closed(self) -> None:
        self._broken = True
        self._stop_worker()

    def _unavailable(self, started: float) -> Observation:
        kind = "WorkerSessionClosedError" if self._closed else "WorkerSessionUnavailableError"
        return Observation(
            outcome=ExecutionOutcome.CRASHED,
            exception=ExceptionInfo(
                module="parity.execution",
                type=kind,
                message="isolated worker session is unavailable",
            ),
            metrics=RunMetrics(duration_seconds=time.perf_counter() - started),
        )

    def execute(
        self,
        invocation: Invocation,
        *,
        timeout_seconds: float | None = None,
        _operation: Literal["execute", "inspect", "runtime"] = "execute",
    ) -> Observation:
        """Execute one call, preserving worker state but never input objects."""

        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        inputs = _normalize_inputs(invocation)
        started = time.perf_counter()
        with self._lock:
            if self._closed or self._broken:
                return self._unavailable(started)
            if _operation == "execute" and not self._runtime_validated:
                preflight = self.preflight_runtime(timeout_seconds=timeout)
                if not preflight.succeeded:
                    return preflight
            try:
                process = self._start()
            except OSError as error:
                self._fail_closed()
                return Observation(
                    outcome=ExecutionOutcome.CRASHED,
                    exception=ExceptionInfo.from_exception(error),
                    metrics=RunMetrics(duration_seconds=time.perf_counter() - started),
                )

            self._counter += 1
            token = f"call-{self._counter:08d}-{secrets.token_hex(16)}"
            call_root = self._root / token
            call_root.mkdir(mode=0o700)
            request_path = call_root / "request.json"
            response_path = call_root / "response.json"
            output_arrow = call_root / "output.arrow"
            output_json = call_root / "output.json"
            try:
                endpoint: dict[str, Any]
                if self.spec.command is not None:
                    endpoint = {
                        "kind": "command",
                        "record_distributions": list(self.spec.provenance_distributions),
                    }
                else:
                    endpoint = {
                        "kind": "python",
                        "target": self.spec.target,
                        "canonicalizer": self.spec.canonicalizer,
                        "adapter": self.spec.adapter,
                        "pandas_input": self.spec.pandas_input,
                        "record_distributions": list(self.spec.provenance_distributions),
                    }
                invocation_payload, input_labels = _write_invocation(inputs, call_root)
                request = {
                    "protocol_version": _TARGET_PROTOCOL_VERSION,
                    "operation": _operation,
                    "endpoint": endpoint,
                    "invocation": invocation_payload,
                    "output": {
                        "arrow": str(output_arrow),
                        "json": str(output_json),
                    },
                }
                request_path.write_text(json.dumps(request), encoding="utf-8")

                try:
                    if process.stdin is None:  # pragma: no cover - Popen contract
                        raise BrokenPipeError
                    process.stdin.write(f"{token}\n".encode("ascii"))
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    returncode = process.poll()
                    self._fail_closed()
                    return Observation(
                        outcome=ExecutionOutcome.CRASHED,
                        exception=ExceptionInfo(
                            module="parity.execution",
                            type="WorkerSessionError",
                            message=f"isolated worker session exited with status {returncode}",
                        ),
                        metrics=RunMetrics(duration_seconds=time.perf_counter() - started),
                    )

                deadline = time.monotonic() + timeout
                with _MemorySampler(process.pid) as memory:
                    while not _response_publication_finished(response_path):
                        returncode = process.poll()
                        if returncode is not None:
                            self._fail_closed()
                            return Observation(
                                outcome=ExecutionOutcome.CRASHED,
                                exception=ExceptionInfo(
                                    module="parity.execution",
                                    type="WorkerSessionError",
                                    message=(
                                        f"isolated worker session exited with status {returncode}"
                                    ),
                                ),
                                metrics=RunMetrics(
                                    duration_seconds=time.perf_counter() - started,
                                    peak_rss_bytes=memory.peak or None,
                                ),
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            if os.path.lexists(response_path):
                                # A stable hard link is invalid rather than an
                                # absent-response timeout. Let the strict reader
                                # classify it below.
                                break
                            self._fail_closed()
                            return Observation(
                                outcome=ExecutionOutcome.TIMED_OUT,
                                exception=ExceptionInfo(
                                    module="parity.execution",
                                    type="TimeoutError",
                                    message=f"implementation exceeded {timeout:g} seconds",
                                ),
                                metrics=RunMetrics(
                                    duration_seconds=time.perf_counter() - started,
                                    peak_rss_bytes=memory.peak or None,
                                ),
                            )
                        time.sleep(min(0.005, remaining))

                try:
                    response_payload = _read_protocol_file(
                        call_root,
                        response_path,
                        expected_name="response.json",
                        max_bytes=_MAX_TARGET_RESPONSE_BYTES,
                    )
                    response: dict[str, Any] = json.loads(response_payload.decode("utf-8"))
                    observation = _observation_from_target(
                        response,
                        call_root,
                        output_arrow,
                        output_json,
                        expected_input_labels=input_labels,
                        allow_outputless_success=_operation in {"inspect", "runtime"},
                        expected_executor=(
                            "command" if self.spec.command is not None else "portable-python"
                        ),
                    )
                except Exception as error:
                    self._fail_closed()
                    return Observation(
                        outcome=ExecutionOutcome.CRASHED,
                        exception=ExceptionInfo(
                            module="parity.execution",
                            type="WorkerProtocolError",
                            message=f"invalid isolated worker response: {type(error).__name__}",
                        ),
                        metrics=RunMetrics(
                            duration_seconds=time.perf_counter() - started,
                            peak_rss_bytes=memory.peak or None,
                        ),
                    )
                if memory.peak:
                    observation.metrics.peak_rss_bytes = max(
                        memory.peak, observation.metrics.peak_rss_bytes or 0
                    )
                if (
                    self._validated_runtime is not None
                    and observation.runtime is not None
                    and diff_runtime(self._validated_runtime, observation.runtime)
                ):
                    self._fail_closed()
                    return Observation(
                        outcome=ExecutionOutcome.ERROR,
                        exception=ExceptionInfo(
                            module="parity.execution",
                            type="RuntimeDriftError",
                            message="target runtime changed after preflight",
                        ),
                        metrics=observation.metrics,
                        runtime=observation.runtime,
                    )
                return observation
            finally:
                # Call directories can contain user values.  Remove them after
                # each response instead of retaining a campaign-sized corpus.
                shutil.rmtree(call_root, ignore_errors=True)

    def inspect_runtime(self, *, timeout_seconds: float | None = None) -> Observation:
        """Validate transport and collect provenance without importing the target."""

        return self.execute(
            Invocation(),
            timeout_seconds=timeout_seconds,
            _operation="runtime",
        )

    def inspect_endpoint(self, *, timeout_seconds: float | None = None) -> Observation:
        """Validate target, canonicalizer, and adapter imports without invocation."""

        return self.execute(
            Invocation(),
            timeout_seconds=timeout_seconds,
            _operation="inspect",
        )

    def preflight_transport(self, *, timeout_seconds: float | None = None) -> Observation:
        """Validate transport, runtime identity, and requirements before user imports."""

        with self._lock:
            if self._closed or self._broken:
                return self._unavailable(time.perf_counter())
            if self._validated_runtime is not None:
                return self.inspect_runtime(timeout_seconds=timeout_seconds)
            observation = self.inspect_runtime(timeout_seconds=timeout_seconds)
            if not observation.succeeded or observation.runtime is None:
                self._fail_closed()
                return _preflight_failure_observation(observation, phase="transport")
            failure = _runtime_contract_observation(
                self.spec,
                observation.runtime,
                metrics=observation.metrics,
            )
            if failure is not None:
                self._fail_closed()
                return failure
            if drift := _runtime_drift_observation(
                self.expected_runtime,
                observation.runtime,
                started=time.perf_counter() - observation.metrics.duration_seconds,
                peak_rss_bytes=observation.metrics.peak_rss_bytes,
            ):
                self._fail_closed()
                return drift
            self._validated_runtime = observation.runtime
            return observation

    def preflight_endpoint(self, *, timeout_seconds: float | None = None) -> Observation:
        """Validate target, canonicalizer, and adapter imports after transport checks."""

        with self._lock:
            if self._closed or self._broken:
                return self._unavailable(time.perf_counter())
            if self._runtime_validated:
                return self.inspect_endpoint(timeout_seconds=timeout_seconds)
            transport = self.preflight_transport(timeout_seconds=timeout_seconds)
            if not transport.succeeded or transport.runtime is None:
                return transport
            endpoint = self.inspect_endpoint(timeout_seconds=timeout_seconds)
            if not endpoint.succeeded or endpoint.runtime is None:
                self._fail_closed()
                return _preflight_failure_observation(
                    endpoint,
                    phase="endpoint",
                    runtime=transport.runtime,
                )
            self._runtime_validated = True
            return endpoint

    def preflight_runtime(self, *, timeout_seconds: float | None = None) -> Observation:
        """Validate transport, imports, runtime identity, and requirements."""

        return self.preflight_endpoint(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        """Terminate this worker and descendants and remove all protocol files."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_worker()
            self._temporary.cleanup()


def execute(
    spec: CallableSpec,
    invocation: Invocation,
    *,
    isolated: bool | None = None,
    timeout_seconds: float = 30.0,
    expected_runtime: RuntimeProvenance | None = None,
) -> Observation:
    """Execute using current or isolated mode.

    Auto mode selects isolation when a Python executable or protocol command is
    configured. Engines that require timeout enforcement should pass
    ``isolated=True`` explicitly.
    """

    use_isolated = (
        spec.python is not None or spec.command is not None if isolated is None else isolated
    )
    if use_isolated:
        return execute_isolated(
            spec,
            invocation,
            timeout_seconds=timeout_seconds,
            expected_runtime=expected_runtime,
        )
    return execute_current(
        spec,
        invocation,
        expected_runtime=expected_runtime,
    )


__all__ = [
    "ExceptionInfo",
    "ExecutionError",
    "ExecutionOutcome",
    "IsolatedExecutionSession",
    "Observation",
    "execute",
    "execute_callable_current",
    "execute_current",
    "execute_isolated",
    "import_callable",
    "redact_text",
]
