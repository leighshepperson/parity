"""Dependency-light Python target worker for Parity's process protocol.

This file is executed by path, not imported as part of :mod:`parity`.  Keep it
compatible with Python 3.8 and limited to the standard library plus the target
environment's selected dataframe adapter.  In particular, it must never import
Pydantic, Hypothesis, Rich, Typer, or Parity itself.

The controller owns the private session directory.  It sends opaque call
directory names over stdin; Arrow/JSON files carry inputs and observations.
Target exceptions are observations.  Import, adaptation, canonicalisation, and
protocol failures are worker errors, because no behavioural comparison can be
made in those cases.
"""

import hashlib
import importlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
import typing
import urllib.parse
import urllib.request

# Import validation must be observational. In particular, do not create
# untracked ``__pycache__`` files inside an editable source worktree.
sys.dont_write_bytecode = True

PROTOCOL_VERSION = 1
_CALL_TOKEN = re.compile(r"^call-[0-9]{8}-[0-9a-f]{32}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~!-]{0,127}$")
_DIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$")
_DIST_SEPARATOR = re.compile(r"[-_.]+")
_IMPORT_TARGET = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_DETAIL_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_API_TOKEN = re.compile(r"\b(?:np|numpy|ndarray)\.[A-Za-z_][A-Za-z0-9_]*\b")
_CORE_DISTRIBUTIONS = ("numpy", "pandas", "polars", "pyarrow")


class WorkerFailure(Exception):
    """A data-safe failure of the worker boundary, not of the target call."""


def _safe_label(value):
    value = str(value)
    return value if _SAFE_LABEL.fullmatch(value) else "unknown"


def _atomic_json(path, value):
    temporary = path + f".{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
    os.replace(temporary, path)


def _load_json(path):
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise WorkerFailure("request must be a JSON object")
    return value


def _within(root, path, expected_name, must_exist=False):
    resolved_root = os.path.realpath(root)
    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != resolved_root or os.path.basename(resolved) != expected_name:
        raise WorkerFailure("protocol path escapes its call directory")
    if must_exist and not os.path.isfile(resolved):
        raise WorkerFailure("required protocol file is missing")
    return resolved


def _normalize_distribution_name(name):
    if not isinstance(name, str) or not _DIST_NAME.fullmatch(name):
        raise WorkerFailure("invalid distribution name")
    return _DIST_SEPARATOR.sub("-", name).lower()


def _distribution_version(name):
    try:
        from importlib import metadata

        version = metadata.version(name)
    except Exception:
        return {"name": name, "status": "missing", "version": None}
    if not isinstance(version, str) or not _SAFE_LABEL.fullmatch(version):
        return {"name": name, "status": "unavailable", "version": None}
    return {"name": name, "status": "installed", "version": version}


def _git_output(root, arguments):
    try:
        completed = subprocess.run(
            ["git", "-C", root, *list(arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, ValueError):
        raise WorkerFailure("editable source identity could not be inspected") from None
    if completed.returncode != 0:
        raise WorkerFailure("editable source identity could not be inspected")
    return completed.stdout


def _git_root(source):
    raw = _git_output(source, ["rev-parse", "--show-toplevel"]).rstrip(b"\r\n")
    try:
        root = os.path.realpath(os.fsdecode(raw))
        if os.path.commonpath([root, os.path.realpath(source)]) != root or not os.path.isdir(root):
            raise ValueError
    except (OSError, ValueError):
        raise WorkerFailure("editable source has an invalid Git worktree") from None
    return root


def _hash_source_entry(digest, root, raw_relative):
    relative_text = os.fsdecode(raw_relative)
    if os.path.isabs(relative_text) or ".." in relative_text.replace("\\", "/").split("/"):
        raise WorkerFailure("Git returned an unsafe source entry")
    path = os.path.join(root, relative_text)
    encoded_path = relative_text.replace(os.sep, "/").encode(sys.getfilesystemencoding())
    digest.update(len(encoded_path).to_bytes(8, "big"))
    digest.update(encoded_path)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError:
        raise WorkerFailure("editable source entry could not be inspected") from None
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        digest.update(b"symlink\0")
        try:
            target = os.fsencode(os.readlink(path))
        except OSError:
            raise WorkerFailure("editable source symlink could not be inspected") from None
        digest.update(len(target).to_bytes(8, "big"))
        digest.update(target)
        return
    if stat.S_ISREG(mode):
        digest.update(b"file\0")
        digest.update(b"executable\0" if mode & 0o111 else b"regular\0")
        try:
            with open(path, "rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            raise WorkerFailure("editable source file could not be read") from None
        return
    if stat.S_ISDIR(mode):
        digest.update(b"directory\0")
        if _git_root(path) == root:
            raise WorkerFailure("editable source contains an uninitialized Git submodule")
        nested = _source_identity(path, None)
        digest.update(nested["revision"].encode("ascii"))
        digest.update(b"dirty\0" if nested["dirty"] else b"clean\0")
        digest.update(nested["sha256"].encode("ascii"))
        return
    raise WorkerFailure("editable source contains an unsupported entry")


def _source_identity(source, name):
    root = _git_root(source)
    head_before = _git_output(root, ["rev-parse", "--verify", "HEAD"]).strip()
    if not re.fullmatch(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?", head_before):
        raise WorkerFailure("Git returned an invalid source revision")
    status_arguments = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ]
    status_before = _git_output(root, status_arguments)

    def tree_digest():
        listed = _git_output(
            root,
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        )
        paths = [item for item in listed.split(b"\0") if item]
        if len(paths) != len(set(paths)):
            raise WorkerFailure("Git returned duplicate source entries")
        digest = hashlib.sha256(b"parity-source-v1\0")
        for raw_path in sorted(paths):
            _hash_source_entry(digest, root, raw_path)
        return digest.hexdigest()

    source_sha256 = tree_digest()
    if (
        _git_output(root, ["rev-parse", "--verify", "HEAD"]).strip() != head_before
        or _git_output(root, status_arguments) != status_before
        or tree_digest() != source_sha256
    ):
        raise WorkerFailure("editable source changed during inspection")
    return {
        "name": name,
        "kind": "git-worktree-v1",
        "revision": head_before.decode("ascii"),
        "dirty": bool(status_before),
        "sha256": source_sha256,
    }


def _editable_identity(name):
    try:
        from importlib import metadata

        distribution = metadata.distribution(name)
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        parsed = urllib.parse.urlsplit(direct.get("url", ""))
        if parsed.scheme != "file" or not direct.get("dir_info", {}).get("editable"):
            return None
        if parsed.netloc not in ("", "localhost"):
            return None
        source = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
        return _source_identity(source, name)
    except WorkerFailure:
        raise
    except Exception:
        return None


def _runtime(distributions):
    names = set(_CORE_DISTRIBUTIONS)
    for name in distributions:
        names.add(_normalize_distribution_name(name))
    identities = []
    for name in sorted(names):
        identity = _editable_identity(name)
        if identity is not None:
            identities.append(identity)
    return {
        "executor": "portable-python",
        "runtime_name": "python",
        "runtime_version": _safe_label(platform.python_version()),
        "python_implementation": _safe_label(platform.python_implementation()),
        "python_version": _safe_label(platform.python_version()),
        "platform_system": _safe_label(platform.system()),
        "platform_machine": _safe_label(platform.machine()),
        "parity_version": None,
        "distributions": [_distribution_version(name) for name in sorted(names)],
        "identities": identities,
    }


def _import_callable(target):
    if not isinstance(target, str) or not _IMPORT_TARGET.fullmatch(target):
        raise WorkerFailure("invalid Python import target")
    module_name, attribute_path = target.split(":", 1)
    try:
        value = importlib.import_module(module_name)
        for part in attribute_path.split("."):
            value = getattr(value, part)
    except Exception:
        raise WorkerFailure("Python target could not be imported") from None
    if not callable(value):
        raise WorkerFailure("Python target is not callable")
    return value


def _arrow_modules():
    try:
        import pyarrow as pa
        import pyarrow.ipc as ipc
    except Exception:
        raise WorkerFailure("portable Python targets require pyarrow") from None
    return pa, ipc


def _read_arrow(path):
    _pa, ipc = _arrow_modules()
    try:
        with open(path, "rb") as stream:
            return ipc.open_file(stream).read_all()
    except Exception:
        raise WorkerFailure("canonical Arrow input could not be read") from None


def _write_arrow(table, path):
    _pa, ipc = _arrow_modules()
    try:
        with open(path, "wb") as stream, ipc.new_file(stream, table.schema) as writer:
            writer.write_table(table)
    except Exception:
        raise WorkerFailure("canonical Arrow output could not be written") from None


def _arrow_bytes(table):
    pa, ipc = _arrow_modules()
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _fresh_arrow(table):
    pa, ipc = _arrow_modules()
    return ipc.open_stream(pa.py_buffer(_arrow_bytes(table))).read_all()


def _from_arrow(table, adapter, pandas_input):
    fresh = _fresh_arrow(table)
    try:
        if adapter == "arrow":
            return fresh
        if adapter == "pandas":
            if pandas_input == "native":
                return fresh.to_pandas()
            import pandas as pd

            return fresh.to_pandas(types_mapper=pd.ArrowDtype)
        if adapter == "polars":
            import polars as pl

            return pl.from_arrow(fresh, rechunk=True)
    except Exception:
        raise WorkerFailure("canonical input could not be adapted") from None
    raise WorkerFailure("portable Python targets require an explicit adapter")


def _pandas_to_arrow(value, preserve_index):
    pa, _ipc = _arrow_modules()
    import numpy as np
    import pandas as pd

    if isinstance(value, pd.Series):
        name = value.name if value.name is not None else "value"
        value = value.to_frame(name=name)
    if not isinstance(value, pd.DataFrame):
        raise WorkerFailure("pandas adapter received an unsupported value")

    # Match the controller's canonical boundary: normalize nested extension
    # scalars, omit indexes from returned values, and preserve IEEE NaN as
    # distinct from a database null in floating domains.
    nested_columns = []
    for position in range(value.shape[1]):
        source = value.iloc[:, position]
        if source.dtype != object:
            continue
        normalized = []
        changed = False
        for item in source.tolist():
            if isinstance(item, pd.api.extensions.ExtensionArray):
                normalized.append(item.tolist())
                changed = True
            else:
                normalized.append(item)
        if changed:
            nested_columns.append((position, normalized))
    if nested_columns:
        value = value.copy()
        for position, normalized in nested_columns:
            value.isetitem(position, pd.Series(normalized, index=value.index, dtype=object))

    table = pa.Table.from_pandas(value, preserve_index=preserve_index).combine_chunks()
    data_column_count = value.shape[1] if preserve_index else len(table.schema)
    for index in range(data_column_count):
        field = table.schema.field(index)
        source = value.iloc[:, index]
        source_values = source.tolist()
        floating_numpy_dtype = isinstance(source.dtype, np.dtype) and np.issubdtype(
            source.dtype, np.floating
        )
        contains_float = any(isinstance(item, (float, np.floating)) for item in source_values)
        if not floating_numpy_dtype and not (
            source.dtype == object
            and contains_float
            and (pa.types.is_floating(field.type) or pa.types.is_null(field.type))
        ):
            continue
        values = [None if item is pd.NA or item is pd.NaT else item for item in source_values]
        target_type = field.type if pa.types.is_floating(field.type) else None
        array = pa.array(values, type=target_type, from_pandas=False)
        table = table.set_column(index, field.with_type(array.type), array)
    return table


def _to_arrow(value, adapter, preserve_index=False):
    pa, _ipc = _arrow_modules()
    try:
        if adapter == "arrow":
            if isinstance(value, pa.RecordBatch):
                return pa.Table.from_batches([value])
            if isinstance(value, pa.Table):
                return value.combine_chunks()
        elif adapter == "pandas":
            import pandas as pd

            if isinstance(value, (pd.DataFrame, pd.Series)):
                return _pandas_to_arrow(value, preserve_index)
        elif adapter == "polars":
            import polars as pl

            if isinstance(value, pl.LazyFrame):
                value = value.collect()
            if isinstance(value, pl.Series):
                value = value.to_frame()
            if isinstance(value, pl.DataFrame):
                return value.to_arrow().combine_chunks()
    except Exception:
        raise WorkerFailure("adapter value could not cross the Arrow boundary") from None
    raise WorkerFailure("adapter received an unsupported dataframe value")


def _return_to_arrow(value):
    pa, _ipc = _arrow_modules()
    if isinstance(value, (pa.Table, pa.RecordBatch)):
        return _to_arrow(value, "arrow")
    try:
        import pandas as pd

        if isinstance(value, (pd.DataFrame, pd.Series)):
            return _to_arrow(value, "pandas")
    except ImportError:
        pass
    try:
        import polars as pl

        if isinstance(value, (pl.DataFrame, pl.LazyFrame, pl.Series)):
            return _to_arrow(value, "polars")
    except ImportError:
        pass
    return None


def _resolve_adapter(requested, function):
    if requested != "auto":
        return requested
    try:
        annotations = typing.get_type_hints(function)
    except Exception:
        annotations = getattr(function, "__annotations__", {})
    for name, annotation in annotations.items():
        if name == "return":
            continue
        module = getattr(annotation, "__module__", "")
        qualified = f"{module}.{getattr(annotation, '__qualname__', annotation)}"
        qualified = qualified.lower()
        if "pandas" in qualified:
            return "pandas"
        if "polars" in qualified:
            return "polars"
        if "pyarrow" in qualified or "arrow" in qualified:
            return "arrow"
        break
    return "pandas"


def _inspect_endpoint(endpoint):
    """Validate imports and transport without invoking the configured target."""

    _arrow_modules()
    target = endpoint.get("target")
    canonicalizer_target = endpoint.get("canonicalizer")
    requested_adapter = endpoint.get("adapter", "auto")
    pandas_input = endpoint.get("pandas_input", "arrow")
    if requested_adapter not in ("auto", "arrow", "pandas", "polars"):
        raise WorkerFailure("unsupported Python adapter")
    if pandas_input not in ("arrow", "native"):
        raise WorkerFailure("invalid pandas input mode")
    function = _import_callable(target)
    if canonicalizer_target is not None:
        _import_callable(canonicalizer_target)
    adapter = _resolve_adapter(requested_adapter, function)
    if adapter == "pandas":
        try:
            import pandas  # noqa: F401
        except Exception:
            raise WorkerFailure("pandas adapter is unavailable") from None
    elif adapter == "polars":
        try:
            import polars  # noqa: F401
        except Exception:
            raise WorkerFailure("polars adapter is unavailable") from None
    elif adapter != "arrow":
        raise WorkerFailure("portable Python targets require an explicit adapter")


def _fingerprint(value, adapter):
    try:
        return hashlib.sha256(_arrow_bytes(_to_arrow(value, adapter, True))).hexdigest()
    except Exception:
        return None


def _json_value(value):
    def valid_keys(item, seen):
        marker = id(item)
        if marker in seen:
            return False
        if isinstance(item, dict):
            seen.add(marker)
            try:
                return all(
                    isinstance(key, str) and valid_keys(nested, seen)
                    for key, nested in item.items()
                )
            finally:
                seen.remove(marker)
        if isinstance(item, (list, tuple)):
            seen.add(marker)
            try:
                return all(valid_keys(nested, seen) for nested in item)
            finally:
                seen.remove(marker)
        return True

    try:
        if not valid_keys(value, set()):
            return False, None
        encoded = json.dumps(value, allow_nan=True)
        return True, json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False, None


def _inputs(request, call_root, adapter, pandas_input):
    envelope = request.get("inputs")
    if not isinstance(envelope, dict) or set(envelope) != {"kind", "items"}:
        raise WorkerFailure("invalid input bundle")
    kind = envelope.get("kind")
    items = envelope.get("items")
    if kind not in ("single", "positional", "keyword") or not isinstance(items, list) or not items:
        raise WorkerFailure("invalid input bundle")
    if kind == "single" and len(items) != 1:
        raise WorkerFailure("single input requires one item")
    parsed = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"name", "path"}:
            raise WorkerFailure("invalid input item")
        name = item.get("name")
        path = item.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            raise WorkerFailure("invalid input item")
        expected_name = f"input-{index:08d}.arrow"
        table = _read_arrow(_within(call_root, path, expected_name, True))
        parsed.append((name, _from_arrow(table, adapter, pandas_input)))
    labels = [name for name, _value in parsed]
    if len(labels) != len(set(labels)):
        raise WorkerFailure("input labels must be unique")
    if kind == "single" and labels != ["input"]:
        raise WorkerFailure("invalid single input label")
    if kind == "positional" and labels != [str(index) for index in range(len(parsed))]:
        raise WorkerFailure("invalid positional input labels")
    if kind == "keyword" and any(not name.isidentifier() for name in labels):
        raise WorkerFailure("invalid keyword input labels")
    return kind, parsed


def _exception_details(error):
    details = {}
    api_tokens = {match.group(0)[:128] for match in _API_TOKEN.finditer(str(error))}
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
        except TypeError:
            try:
                raw_errors = errors_method()
            except Exception:
                raw_errors = None
        except Exception:
            raw_errors = None
        if isinstance(raw_errors, (list, tuple)):
            codes = set()
            locations = set()
            for item in raw_errors[:32]:
                if not isinstance(item, dict):
                    continue
                code = item.get("type")
                if isinstance(code, str) and _DETAIL_TOKEN.fullmatch(code):
                    codes.add(code)
                location = item.get("loc")
                if isinstance(location, (list, tuple)):
                    parts = []
                    for part in location[:16]:
                        if isinstance(part, int):
                            parts.append("index")
                        elif isinstance(part, str):
                            parts.append("field")
                        else:
                            parts.append("item")
                    locations.add("/".join(parts) or "root")
            if codes:
                details["error_codes"] = sorted(codes)
            if locations:
                details["location_shapes"] = sorted(locations)
    return details


def _exception(error):
    return {
        "module": _safe_label(type(error).__module__),
        "type": _safe_label(type(error).__qualname__),
        # This remains inside a private call directory.  The controller owns
        # normalization, redaction, length bounds, and semantic fingerprinting.
        "message": str(error)[:16384],
        "details": _exception_details(error),
    }


def _worker_error(error, duration_seconds, runtime, mutated_inputs=None, return_type=None):
    return {
        "outcome": "error",
        "duration_seconds": duration_seconds,
        "exception": {
            "module": "parity.execution",
            "type": "ExecutionError",
            "message": str(error),
            "details": {},
        },
        "mutated_inputs": list(mutated_inputs or []),
        "return_type": return_type,
        "runtime": runtime,
        "output": None,
    }


def _invoke(function, kind, parsed, static_args, static_kwargs):
    values = [value for _name, value in parsed]
    if kind == "single":
        return function(values[0], *static_args, **static_kwargs)
    if kind == "positional":
        return function(*(values + list(static_args)), **static_kwargs)
    if static_args:
        raise WorkerFailure("keyword inputs cannot be combined with static positional args")
    keywords = dict(parsed)
    if set(keywords).intersection(static_kwargs):
        raise WorkerFailure("input names collide with static keyword args")
    keywords.update(static_kwargs)
    return function(**keywords)


def _execute(request, call_root, endpoint, runtime):
    target = endpoint.get("target")
    canonicalizer_target = endpoint.get("canonicalizer")
    requested_adapter = endpoint.get("adapter", "auto")
    pandas_input = endpoint.get("pandas_input", "arrow")
    if requested_adapter not in ("auto", "arrow", "pandas", "polars"):
        raise WorkerFailure("unsupported Python adapter")
    if pandas_input not in ("arrow", "native"):
        raise WorkerFailure("invalid pandas input mode")
    static_args = request.get("static_args", [])
    static_kwargs = request.get("static_kwargs", {})
    if not isinstance(static_args, list) or not isinstance(static_kwargs, dict):
        raise WorkerFailure("invalid static arguments")
    if not all(isinstance(name, str) for name in static_kwargs):
        raise WorkerFailure("static keyword names must be strings")
    function = _import_callable(target)
    if canonicalizer_target is not None and not isinstance(canonicalizer_target, str):
        raise WorkerFailure("invalid output canonicalizer")
    canonicalizer = (
        _import_callable(canonicalizer_target) if canonicalizer_target is not None else None
    )
    adapter = _resolve_adapter(requested_adapter, function)
    kind, parsed = _inputs(request, call_root, adapter, pandas_input)
    before = [_fingerprint(value, adapter) for _name, value in parsed]
    started = time.perf_counter()
    try:
        returned = _invoke(function, kind, parsed, static_args, static_kwargs)
    except BaseException as error:
        mutated = [
            name
            for (name, value), fingerprint in zip(parsed, before)  # noqa: B905 (Python 3.8)
            if fingerprint != _fingerprint(value, adapter)
        ]
        return {
            "outcome": "raised",
            "duration_seconds": time.perf_counter() - started,
            "exception": _exception(error),
            "mutated_inputs": mutated,
            "return_type": None,
            "runtime": runtime,
            "output": None,
        }
    mutated = [
        name
        for (name, value), fingerprint in zip(parsed, before)  # noqa: B905 (Python 3.8)
        if fingerprint != _fingerprint(value, adapter)
    ]
    return_type = f"{type(returned).__module__}.{type(returned).__qualname__}"
    try:
        if canonicalizer is not None:
            returned = canonicalizer(returned)
        table = _return_to_arrow(returned)
        output = request.get("output")
        if not isinstance(output, dict) or set(output) != {"arrow", "json"}:
            raise WorkerFailure("invalid output paths")
        if table is not None:
            path = _within(call_root, output.get("arrow"), "output.arrow")
            _write_arrow(table, path)
            output_metadata = {"kind": "arrow"}
        else:
            serializable, value = _json_value(returned)
            if not serializable:
                raise WorkerFailure("target return could not be canonicalized")
            path = _within(call_root, output.get("json"), "output.json")
            _atomic_json(path, value)
            output_metadata = {"kind": "json"}
    except WorkerFailure as error:
        return _worker_error(
            error,
            time.perf_counter() - started,
            runtime,
            mutated_inputs=mutated,
            return_type=return_type,
        )
    except BaseException:
        return _worker_error(
            WorkerFailure("target output canonicalizer failed"),
            time.perf_counter() - started,
            runtime,
            mutated_inputs=mutated,
            return_type=return_type,
        )
    return {
        "outcome": "returned",
        "duration_seconds": time.perf_counter() - started,
        "exception": None,
        "mutated_inputs": mutated,
        "return_type": return_type,
        "runtime": runtime,
        "output": output_metadata,
    }


def run_request(request_path, response_path):
    call_root = os.path.dirname(os.path.realpath(request_path))
    request_path = _within(call_root, request_path, "request.json", True)
    response_path = _within(call_root, response_path, "response.json")
    request = _load_json(request_path)
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise WorkerFailure("unsupported target protocol")
    endpoint = request.get("endpoint")
    if not isinstance(endpoint, dict):
        raise WorkerFailure("invalid endpoint")
    distributions = endpoint.get("record_distributions", [])
    if not isinstance(distributions, list):
        raise WorkerFailure("invalid distribution list")
    runtime = _runtime(distributions)
    operation = request.get("operation")
    if operation in ("runtime", "inspect", "execute"):
        started = time.perf_counter()
        try:
            if operation == "runtime":
                _arrow_modules()
                response = {
                    "outcome": "returned",
                    "duration_seconds": time.perf_counter() - started,
                    "exception": None,
                    "mutated_inputs": [],
                    "return_type": None,
                    "runtime": runtime,
                    "output": None,
                }
            elif operation == "inspect":
                _inspect_endpoint(endpoint)
                response = {
                    "outcome": "returned",
                    "duration_seconds": time.perf_counter() - started,
                    "exception": None,
                    "mutated_inputs": [],
                    "return_type": None,
                    "runtime": runtime,
                    "output": None,
                }
            else:
                response = _execute(request, call_root, endpoint, runtime)
        except WorkerFailure as error:
            response = _worker_error(error, time.perf_counter() - started, runtime)
    else:
        raise WorkerFailure("unsupported target operation")
    response["protocol_version"] = PROTOCOL_VERSION
    _atomic_json(response_path, response)


def run_session(root):
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        raise WorkerFailure("session root does not exist")
    # Executing this file by absolute path puts Parity's package directory on
    # sys.path. Remove it before user imports so a generic target dependency
    # such as ``models`` cannot fall through to a controller module.
    worker_directory = os.path.realpath(os.path.dirname(__file__))
    sys.path[:] = [
        entry for entry in sys.path if os.path.realpath(entry or os.getcwd()) != worker_directory
    ]
    # The configured working directory is the target's explicit import root.
    working_directory = os.getcwd()
    if working_directory not in sys.path:
        sys.path.insert(0, working_directory)
    for raw_token in sys.stdin.buffer:
        token = raw_token.rstrip(b"\r\n").decode("ascii")
        if not _CALL_TOKEN.fullmatch(token):
            raise WorkerFailure("invalid call token")
        call_root = os.path.join(root, token)
        run_request(
            os.path.join(call_root, "request.json"),
            os.path.join(call_root, "response.json"),
        )


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 2
    try:
        run_session(arguments[0])
    except BaseException:
        # Protocol diagnostics can contain paths or target data.  The controller
        # reports a bounded infrastructure failure; this process stays silent.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
