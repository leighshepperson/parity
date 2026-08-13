from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pytest

from parity.execution import (
    ExecutionError,
    ExecutionOutcome,
    IsolatedExecutionSession,
    execute,
    execute_callable_current,
    execute_current,
    execute_isolated,
    import_callable,
    redact_text,
)
from parity.models import CallableSpec


@pytest.fixture
def transform_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    module = tmp_path / "parity_test_transforms.py"
    module.write_text(
        """
import os
import subprocess
import sys
import time
import pandas as pd
import polars as pl
import pyarrow as pa

def pandas_add(frame: pd.DataFrame, amount=1, *, column='x'):
    result = frame.copy()
    result[column] = result[column] + amount
    return result

def pandas_mutate(frame: pd.DataFrame):
    frame.loc[0, 'x'] = 999
    return frame

def polars_add(frame: pl.DataFrame, amount=1):
    return frame.with_columns((pl.col('x') + amount).alias('x'))

def arrow_identity(frame: pa.Table):
    return frame

def scalar(frame):
    return {'rows': len(frame), 'ok': True}

def explode(frame):
    raise ValueError('/private/customer/input.csv API_TOKEN=abc')

def wait(frame, seconds):
    time.sleep(seconds)
    return frame

def spawn_and_wait(frame, pid_file):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    with open(pid_file, 'w', encoding='utf-8') as stream:
        stream.write(str(child.pid))
    time.sleep(30)
    return frame

def spawn_and_return(frame, pid_file):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    with open(pid_file, 'w', encoding='utf-8') as stream:
        stream.write(str(child.pid))
    return frame

_state_counter = 0

def stateful_and_mutate(frame):
    global _state_counter
    _state_counter += 1
    original = int(frame.loc[0, 'x'])
    frame.loc[0, 'x'] = 999
    return {'call': _state_counter, 'input': original}

def hard_crash(frame):
    os._exit(23)

not_callable = 3
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def _table() -> pa.Table:
    return pa.table({"x": [1, 2], "name": ["a", "b"]})


def _isolated_spec(directory: Path, target: str, adapter: str = "auto") -> CallableSpec:
    source = Path(__file__).parents[1] / "src"
    pythonpath = os.pathsep.join([str(directory), str(source), os.environ.get("PYTHONPATH", "")])
    return CallableSpec(
        target=target,
        adapter=adapter,
        python=Path(sys.executable),
        workdir=directory,
        environment={"PYTHONPATH": pythonpath},
    )


def test_import_callable_validates_target(transform_module: Path) -> None:
    assert callable(import_callable("parity_test_transforms:pandas_add"))
    with pytest.raises(ExecutionError, match=r"module\.path:function\.path"):
        import_callable("parity_test_transforms.pandas_add")
    with pytest.raises(ExecutionError, match="not callable"):
        import_callable("parity_test_transforms:not_callable")


@pytest.mark.parametrize(
    ("target", "adapter"),
    [
        ("parity_test_transforms:pandas_add", "pandas"),
        ("parity_test_transforms:polars_add", "polars"),
        ("parity_test_transforms:arrow_identity", "arrow"),
    ],
)
def test_execute_current_adapts_arrow_and_returns_arrow(
    transform_module: Path, target: str, adapter: str
) -> None:
    observation = execute_current(
        CallableSpec(target=target, adapter=adapter),
        _table(),
        static_args=[2] if "add" in target else [],
    )
    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.table is not None
    expected = [3, 4] if "add" in target else [1, 2]
    assert observation.table.column("x").to_pylist() == expected
    assert observation.metrics.duration_seconds >= 0
    if sys.platform != "win32":
        assert observation.metrics.peak_rss_bytes is not None
        assert observation.metrics.peak_rss_bytes > 0
    assert not observation.mutated_input


def test_auto_adapter_uses_annotation_and_static_kwargs(transform_module: Path) -> None:
    observation = execute_current(
        CallableSpec(target="parity_test_transforms:polars_add"), _table(), static_args=[4]
    )
    assert observation.table is not None
    assert observation.table.column("x").to_pylist() == [5, 6]

    pandas = execute_current(
        CallableSpec(target="parity_test_transforms:pandas_add"),
        _table(),
        static_kwargs={"amount": 5, "column": "x"},
    )
    assert pandas.table is not None
    assert pandas.table.column("x").to_pylist() == [6, 7]


def test_execute_live_non_importable_callable() -> None:
    offset = 8

    def local(frame, *, multiplier=1):
        result = frame.copy()
        result["x"] = (result["x"] + offset) * multiplier
        return result

    observation = execute_callable_current(
        local, _table(), adapter="pandas", static_kwargs={"multiplier": 2}
    )
    assert observation.succeeded
    assert observation.table is not None
    assert observation.table.column("x").to_pylist() == [18, 20]


def test_pandas_output_preserves_nan_distinct_from_null() -> None:
    import pandas as pd

    def missing_values(frame):
        return pd.DataFrame({"x": pd.Series([float("nan"), None, 1.0], dtype=object)})

    observation = execute_callable_current(missing_values, _table(), adapter="pandas")
    assert observation.table is not None
    column = observation.table.column("x")
    assert column.null_count == 1
    assert column[0].as_py() != column[0].as_py()
    assert column[1].as_py() is None
    assert column[2].as_py() == 1


def test_mutation_exception_and_json_return_are_observed(transform_module: Path) -> None:
    mutated = execute_current(
        CallableSpec(target="parity_test_transforms:pandas_mutate", adapter="pandas"), _table()
    )
    assert mutated.succeeded
    assert mutated.mutated_input

    raised = execute_current(
        CallableSpec(target="parity_test_transforms:explode", adapter="pandas"), _table()
    )
    assert raised.outcome is ExecutionOutcome.RAISED
    assert raised.exception is not None
    assert raised.exception.type == "ValueError"
    assert "/private/customer" not in raised.exception.message
    assert "abc" not in raised.exception.message

    scalar = execute_current(
        CallableSpec(target="parity_test_transforms:scalar", adapter="pandas"), _table()
    )
    assert scalar.succeeded
    assert scalar.has_value
    assert scalar.value == {"rows": 2, "ok": True}
    assert scalar.to_metadata()["has_value"] is True
    assert "value" not in scalar.to_metadata()


def test_execute_isolated_round_trips_and_honours_workdir(transform_module: Path) -> None:
    observation = execute_isolated(
        _isolated_spec(transform_module, "parity_test_transforms:polars_add", adapter="polars"),
        _table(),
        static_args=[7],
        timeout_seconds=5,
    )
    assert observation.succeeded
    assert observation.table is not None
    assert observation.table.column("x").to_pylist() == [8, 9]
    assert observation.metrics.peak_rss_bytes is None or observation.metrics.peak_rss_bytes > 0


def test_isolated_workers_apply_relative_workdir_once(
    transform_module: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(transform_module.parent)
    spec = CallableSpec(
        target="parity_test_transforms:scalar",
        adapter="pandas",
        workdir=Path(transform_module.name),
    )

    disposable = execute_isolated(spec, _table(), timeout_seconds=5)
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        persistent = session.execute(_table())

    assert disposable.succeeded
    assert persistent.succeeded
    assert disposable.value == persistent.value == {"rows": 2, "ok": True}


def test_isolated_session_matches_fresh_worker_observation(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:polars_add", adapter="polars")
    fresh = execute_isolated(spec, _table(), static_args=[7], timeout_seconds=5)
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        persistent = session.execute(_table(), static_args=[7])

    assert persistent.outcome is fresh.outcome is ExecutionOutcome.RETURNED
    assert persistent.table is not None
    assert fresh.table is not None
    assert persistent.table.equals(fresh.table)
    assert persistent.mutated_input is fresh.mutated_input
    assert persistent.return_type == fresh.return_type


def test_isolated_session_preserves_module_state_but_refreshes_each_input(
    transform_module: Path,
) -> None:
    spec = _isolated_spec(
        transform_module, "parity_test_transforms:stateful_and_mutate", adapter="pandas"
    )
    session = IsolatedExecutionSession(spec, timeout_seconds=5)
    with session:
        first = session.execute(_table())
        second = session.execute(pa.table({"x": [41], "name": ["fresh"]}))

    assert first.succeeded
    assert first.mutated_input
    assert second.succeeded
    assert second.mutated_input
    assert first.value == {"call": 1, "input": 1}
    assert second.value == {"call": 2, "input": 41}
    assert session.closed
    unavailable = session.execute(_table())
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionClosedError"


def test_isolated_session_crash_fails_closed(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:hard_crash", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        crashed = session.execute(_table())
        unavailable = session.execute(_table())

    assert crashed.outcome is ExecutionOutcome.CRASHED
    assert crashed.exception is not None
    assert crashed.exception.type == "WorkerSessionError"
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


def test_isolated_session_timeout_fails_closed(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:wait", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=0.2) as session:
        timed_out = session.execute(_table(), static_args=[30])
        unavailable = session.execute(_table())

    assert timed_out.outcome is ExecutionOutcome.TIMED_OUT
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


def test_isolated_session_malformed_response_fails_closed(
    transform_module: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_loads = __import__("parity.execution", fromlist=["json"]).json.loads

    def malformed(payload: str):
        if '"outcome"' in payload:
            return []
        return original_loads(payload)

    monkeypatch.setattr("parity.execution.json.loads", malformed)
    spec = _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        malformed_response = session.execute(_table())
        unavailable = session.execute(_table())

    assert malformed_response.outcome is ExecutionOutcome.CRASHED
    assert malformed_response.exception is not None
    assert malformed_response.exception.type == "WorkerProtocolError"
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


@pytest.mark.integration
def test_isolated_session_close_kills_descendants(transform_module: Path) -> None:
    pid_file = transform_module / "session-child.pid"
    spec = _isolated_spec(
        transform_module, "parity_test_transforms:spawn_and_return", adapter="pandas"
    )
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        observation = session.execute(_table(), static_args=[str(pid_file)])
        assert observation.succeeded
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        # psutil PID visibility can be namespace-limited in containerized test
        # runners even though POSIX signal lookup can see the child.
        os.kill(child_pid, 0)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("session descendant survived explicit cleanup")


@pytest.mark.integration
def test_execute_isolated_times_out_and_kills_descendants(transform_module: Path) -> None:
    psutil = pytest.importorskip("psutil")
    pid_file = transform_module / "child.pid"
    observation = execute_isolated(
        _isolated_spec(transform_module, "parity_test_transforms:spawn_and_wait", adapter="pandas"),
        _table(),
        static_args=[str(pid_file)],
        timeout_seconds=2.0,
    )
    assert observation.outcome is ExecutionOutcome.TIMED_OUT
    assert observation.exception is not None
    deadline = time.monotonic() + 2
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not psutil.pid_exists(child_pid)


def test_execute_dispatch_and_invalid_python(transform_module: Path) -> None:
    isolated = execute(
        _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas"),
        _table(),
    )
    assert isolated.succeeded
    assert isolated.value == {"rows": 2, "ok": True}
    with pytest.raises(ExecutionError, match="isolated"):
        execute_current(
            CallableSpec(target="parity_test_transforms:scalar", python=Path("/different/python")),
            _table(),
        )


def test_redaction_removes_paths_and_secret_assignments() -> None:
    redacted = redact_text("failed at /srv/customer/a.csv with API_KEY=hunter2")
    assert "/srv/customer" not in redacted
    assert "hunter2" not in redacted
    assert "<path>" in redacted
    assert "<redacted>" in redacted
