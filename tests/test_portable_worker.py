from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from parity.execution import _read_arrow, _write_arrow


def _portable_worker() -> Path:
    return Path(__file__).parents[1] / "src" / "parity" / "portable_worker.py"


def _request(call_root: Path, *, target: str) -> dict[str, object]:
    input_path = call_root / "input-00000000.arrow"
    _write_arrow(pa.table({"x": [1, 2]}), input_path)
    return {
        "protocol_version": 2,
        "operation": "execute",
        "endpoint": {
            "target": target,
            "adapter": "pandas",
            "pandas_input": "native",
            "record_distributions": [],
        },
        "invocation": {
            "args": [{"kind": "arrow", "path": str(input_path)}],
            "kwargs": {},
        },
        "output": {
            "arrow": str(call_root / "output.arrow"),
            "json": str(call_root / "output.json"),
        },
    }


def _run_call(tmp_path: Path, request: dict[str, object]) -> dict[str, object]:
    root = tmp_path / "session"
    token = "call-00000001-0123456789abcdef0123456789abcdef"
    call_root = root / token
    call_root.mkdir(parents=True, exist_ok=True)
    (call_root / "request.json").write_text(json.dumps(request), encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(_portable_worker()), str(root)],
        input=f"{token}\n".encode(),
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    return json.loads((call_root / "response.json").read_text(encoding="utf-8"))


def test_portable_worker_has_no_parity_or_controller_dependency_imports() -> None:
    source = _portable_worker().read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.partition(".")[0])

    assert imported_roots.isdisjoint({"parity", "pydantic", "hypothesis", "rich", "typer"})


def test_portable_worker_executes_without_importing_parity(tmp_path: Path) -> None:
    (tmp_path / "portable_target.py").write_text(
        "import sys\n"
        "def transform(frame):\n"
        "    assert 'parity' not in sys.modules\n"
        "    result = frame.copy()\n"
        "    result['x'] += 4\n"
        "    return result\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:transform")

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "returned"
    assert response["protocol_version"] == 2
    assert response["runtime"]["executor"] == "portable-python"
    assert response["runtime"]["parity_version"] is None
    assert _read_arrow(call_root / "output.arrow").column("x").to_pylist() == [5, 6]


def test_portable_worker_auto_adapter_supports_json_without_pandas(tmp_path: Path) -> None:
    (tmp_path / "portable_target.py").write_text(
        "import sys\n"
        "def summarize(values: list[int]):\n"
        "    assert 'pandas' not in sys.modules\n"
        "    return {'total': sum(values)}\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = {
        "protocol_version": 2,
        "operation": "execute",
        "endpoint": {
            "target": "portable_target:summarize",
            "adapter": "auto",
            "pandas_input": "arrow",
            "record_distributions": [],
        },
        "invocation": {
            "args": [{"kind": "json", "value": [1, 2, 3]}],
            "kwargs": {},
        },
        "output": {
            "arrow": str(call_root / "output.arrow"),
            "json": str(call_root / "output.json"),
        },
    }

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "returned"
    assert json.loads((call_root / "output.json").read_text(encoding="utf-8")) == {"total": 6}


def test_portable_worker_distinguishes_target_raise_from_worker_failure(tmp_path: Path) -> None:
    (tmp_path / "portable_target.py").write_text(
        "def raises(_frame):\n    raise ValueError('bad account 42')\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:raises")

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "raised"
    assert response["exception"]["type"] == "ValueError"
    assert response["exception"]["message"] == "bad account 42"

    broken_root = tmp_path / "broken-session"
    broken_call = broken_root / "call-00000001-0123456789abcdef0123456789abcdef"
    broken_call.mkdir(parents=True)
    broken = _request(broken_call, target="missing_module:transform")
    token = broken_call.name
    (broken_call / "request.json").write_text(json.dumps(broken), encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(_portable_worker()), str(broken_root)],
        input=f"{token}\n".encode(),
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    broken_response = json.loads((broken_call / "response.json").read_text(encoding="utf-8"))
    assert broken_response["outcome"] == "error"
    assert broken_response["exception"]["type"] == "ExecutionError"
    assert "missing_module" not in json.dumps(broken_response)
    assert completed.stdout == completed.stderr == b""


def test_portable_worker_extracts_safe_removed_api_tokens(tmp_path: Path) -> None:
    (tmp_path / "portable_target.py").write_text(
        "def raises(_frame):\n"
        "    raise AttributeError('np.product was removed for private customer value')\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)

    response = _run_call(tmp_path, _request(call_root, target="portable_target:raises"))

    assert response["outcome"] == "raised"
    assert response["exception"]["details"] == {"api_tokens": ["np.product"]}


def test_portable_worker_applies_a_small_output_canonicalizer(tmp_path: Path) -> None:
    (tmp_path / "portable_target.py").write_text(
        "class Result:\n"
        "    def __init__(self, total):\n"
        "        self.total = total\n"
        "def transform(frame):\n"
        "    return Result(int(frame['x'].sum()))\n"
        "def canonicalize(result):\n"
        "    return {'total': result.total}\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:transform")
    endpoint = request["endpoint"]
    assert isinstance(endpoint, dict)
    endpoint["canonicalizer"] = "portable_target:canonicalize"

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "returned"
    assert response["return_type"] == "portable_target.Result"
    assert json.loads((call_root / "output.json").read_text(encoding="utf-8")) == {"total": 3}


def test_portable_worker_rejects_input_path_escape_without_disclosing_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "portable_target.py").write_text(
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:transform")
    outside = tmp_path / "outside.arrow"
    _write_arrow(pa.table({"secret": [42]}), outside)
    invocation = request["invocation"]
    assert isinstance(invocation, dict)
    arguments = invocation["args"]
    assert isinstance(arguments, list)
    arguments[0]["path"] = str(outside)

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "error"
    assert response["exception"]["type"] == "ExecutionError"
    assert str(outside) not in json.dumps(response)
    assert not (call_root / "output.arrow").exists()


@pytest.mark.parametrize(
    "invocation",
    [
        {"args": {}, "kwargs": {}},
        {"args": [{"kind": "unknown"}], "kwargs": {}},
        {"args": [], "kwargs": {"not-valid": {"kind": "json", "value": 1}}},
    ],
)
def test_portable_worker_rejects_malformed_invocation_envelopes(
    tmp_path: Path,
    invocation: dict[str, object],
) -> None:
    (tmp_path / "portable_target.py").write_text(
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:transform")
    request["invocation"] = invocation

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "error"
    assert response["exception"]["type"] == "ExecutionError"


def test_portable_worker_bad_session_protocol_is_silent(tmp_path: Path) -> None:
    root = tmp_path / "session"
    root.mkdir()
    completed = subprocess.run(
        [sys.executable, str(_portable_worker()), str(root)],
        input=b"../../escape\n",
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == completed.stderr == b""


def test_portable_worker_import_path_contains_only_the_target_root(
    tmp_path: Path,
) -> None:
    worker_directory = _portable_worker().parent.resolve()
    (tmp_path / "portable_target.py").write_text(
        "import os\n"
        "import sys\n"
        f"FORBIDDEN = {str(worker_directory)!r}\n"
        "def transform(_frame):\n"
        "    paths = [os.path.realpath(item or os.getcwd()) for item in sys.path]\n"
        "    return {'has_workdir': os.path.realpath(os.getcwd()) in paths, "
        "'has_controller_package': os.path.realpath(FORBIDDEN) in paths}\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:transform")

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "returned"
    assert json.loads((call_root / "output.json").read_text(encoding="utf-8")) == {
        "has_controller_package": False,
        "has_workdir": True,
    }


def test_portable_worker_preserves_nested_pydantic_exception_structure(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pydantic")
    (tmp_path / "portable_target.py").write_text(
        "from pydantic import BaseModel\n"
        "class Position(BaseModel):\n"
        "    quantity: int\n"
        "class Portfolio(BaseModel):\n"
        "    position: Position\n"
        "def transform(_frame):\n"
        "    return Portfolio.model_validate({'position': {'quantity': 'not-an-int'}})\n",
        encoding="utf-8",
    )
    root = tmp_path / "session"
    call_root = root / "call-00000001-0123456789abcdef0123456789abcdef"
    call_root.mkdir(parents=True)
    request = _request(call_root, target="portable_target:transform")

    response = _run_call(tmp_path, request)

    assert response["outcome"] == "raised"
    assert response["exception"]["type"] == "ValidationError"
    assert response["exception"]["details"]["location_shapes"] == ["field/field"]
    assert response["exception"]["details"]["error_codes"]
