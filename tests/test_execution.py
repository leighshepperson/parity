from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pytest

import parity.execution as execution_module
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
from parity.invocation import Invocation
from parity.models import CallableSpec, PandasInput


def test_isolated_environment_does_not_inject_wheel_site_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel_module = tmp_path / "site-packages" / "parity" / "execution.py"
    monkeypatch.setattr(execution_module, "__file__", str(wheel_module))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    spec = CallableSpec(
        target="package:callable",
        environment={"PYTHONPATH": "/candidate/site-packages"},
    )

    environment = execution_module._isolated_environment(spec)

    assert environment["PYTHONPATH"] == "/candidate/site-packages"


def test_isolated_environment_never_injects_controller_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "project" / "src"
    source_module = source_root / "parity" / "execution.py"
    source_root.parent.mkdir(parents=True)
    (source_root.parent / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(execution_module, "__file__", str(source_module))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    spec = CallableSpec(
        target="package:callable",
        environment={"PYTHONPATH": "/candidate/site-packages"},
    )

    environment = execution_module._isolated_environment(spec)

    assert environment["PYTHONPATH"] == "/candidate/site-packages"
    assert str(source_root) not in environment["PYTHONPATH"]


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

def pandas_complex_output(_frame: pd.DataFrame):
    return pd.DataFrame({'complex': [1 + 2j]})

def pandas_positional_join(left: pd.DataFrame, right: pd.DataFrame, amount=0):
    result = left.merge(right, on='key')
    result['total'] = result['left'] + result['right'] + amount
    return result

def pandas_keyword_join(*, left: pd.DataFrame, right: pd.DataFrame, amount=0):
    return pandas_positional_join(left, right, amount)

def pandas_mutate_right_then_raise(left: pd.DataFrame, right: pd.DataFrame):
    right.loc[0, 'right'] = 999
    raise RuntimeError('bundle failed')

def polars_add(frame: pl.DataFrame, amount=1):
    return frame.with_columns((pl.col('x') + amount).alias('x'))

def arrow_identity(frame: pa.Table):
    return frame

def scalar(frame):
    return {'rows': len(frame), 'ok': True}

def pandas_input_profile(frame):
    return {
        'integer_dtype': str(frame['integer'].dtype),
        'floating_dtype': str(frame['floating'].dtype),
        'floating_isna': [bool(value) for value in frame['floating'].isna().tolist()],
        'string_dtype': str(frame['text'].dtype),
        'boolean_dtype': str(frame['flag'].dtype),
        'timestamp_dtype': str(frame['when'].dtype),
    }

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


def _call(*args: object, **kwargs: object) -> Invocation:
    return Invocation(args=args, kwargs=kwargs)


def _json_total(values: list[int]) -> dict[str, int]:
    return {"total": sum(values)}


def _join_tables() -> tuple[pa.Table, pa.Table]:
    return (
        pa.table({"key": [1, 2], "left": [10, 20]}),
        pa.table({"key": [1, 2], "right": [1, 2]}),
    )


def _pandas_edge_table() -> pa.Table:
    return pa.table(
        {
            "integer": pa.array([1, None], type=pa.int64()),
            "floating": pa.array([None, float("nan")], type=pa.float64(), from_pandas=False),
            "text": pa.array(["a", None], type=pa.string()),
            "flag": pa.array([True, None], type=pa.bool_()),
            "when": pa.array([0, None], type=pa.timestamp("us")),
        }
    )


def _dense_union_table() -> pa.Table:
    values = pa.UnionArray.from_dense(
        pa.array([0, 1, 0], type=pa.int8()),
        pa.array([0, 0, 1], type=pa.int32()),
        [pa.array([1, 2], type=pa.int64()), pa.array(["text"])],
        field_names=["number", "text"],
    )
    return pa.Table.from_arrays([values], names=["mixed"])


def _isolated_spec(
    directory: Path,
    target: str,
    adapter: str = "auto",
    pandas_input: PandasInput = "arrow",
) -> CallableSpec:
    source = Path(__file__).parents[1] / "src"
    pythonpath = os.pathsep.join([str(directory), str(source), os.environ.get("PYTHONPATH", "")])
    return CallableSpec(
        target=target,
        adapter=adapter,
        pandas_input=pandas_input,
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

    for malformed in ("pkg..module:run", "pkg.:run", "pkg:attr..child"):
        with pytest.raises(ExecutionError, match=r"module\.path:function\.path"):
            import_callable(malformed)


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
        _call(_table(), *([2] if "add" in target else [])),
    )
    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.table is not None
    expected = [3, 4] if "add" in target else [1, 2]
    assert observation.table.column("x").to_pylist() == expected
    assert observation.metrics.duration_seconds >= 0
    if sys.platform != "win32":
        assert observation.metrics.peak_rss_bytes is not None
        assert observation.metrics.peak_rss_bytes > 0
    assert observation.mutated_inputs == ()


def test_auto_adapter_uses_annotation_with_positional_and_keyword_values(
    transform_module: Path,
) -> None:
    observation = execute_current(
        CallableSpec(target="parity_test_transforms:polars_add"), _call(_table(), 4)
    )
    assert observation.table is not None
    assert observation.table.column("x").to_pylist() == [5, 6]
    pandas = execute_current(
        CallableSpec(target="parity_test_transforms:pandas_add"),
        _call(_table(), amount=5, column="x"),
    )
    assert pandas.table is not None
    assert pandas.table.column("x").to_pylist() == [6, 7]


def test_auto_adapter_uses_dependency_light_fallback_for_json_calls() -> None:
    assert execution_module._resolve_adapter("auto", _json_total) == "arrow"


def test_execute_current_invokes_complete_positional_and_keyword_calls(
    transform_module: Path,
) -> None:
    left, right = _join_tables()
    positional = execute_current(
        CallableSpec(target="parity_test_transforms:pandas_positional_join", adapter="pandas"),
        _call(left, right, 5),
    )
    named = execute_current(
        CallableSpec(target="parity_test_transforms:pandas_keyword_join", adapter="pandas"),
        _call(left=left, right=right, amount=7),
    )

    assert positional.succeeded
    assert positional.table is not None
    assert positional.table.column("total").to_pylist() == [16, 27]
    assert positional.mutated_inputs == ()
    assert named.succeeded
    assert named.table is not None
    assert named.table.column("total").to_pylist() == [18, 29]
    assert named.mutated_inputs == ()


def test_execution_requires_an_explicit_valid_invocation(transform_module: Path) -> None:
    left, right = _join_tables()
    spec = CallableSpec(target="parity_test_transforms:pandas_keyword_join", adapter="pandas")
    with pytest.raises(ExecutionError, match=r"parity\.Invocation"):
        execute_current(spec, {"left": left, "right": right})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identifiers"):
        _call(**{"not-valid": left})


def test_bundle_mutation_reports_the_affected_input_even_when_callable_raises(
    transform_module: Path,
) -> None:
    left, right = _join_tables()
    observation = execute_current(
        CallableSpec(
            target="parity_test_transforms:pandas_mutate_right_then_raise", adapter="pandas"
        ),
        _call(left=left, right=right),
    )

    assert observation.outcome is ExecutionOutcome.RAISED
    assert observation.mutated_inputs == ("kwargs/right",)
    assert observation.to_metadata()["mutated_inputs"] == ["kwargs/right"]
    assert "mutated_input" not in observation.to_metadata()


def test_live_callable_accepts_positional_bundle_and_materializes_each_input() -> None:
    left, right = _join_tables()

    def combine(first, second):
        first.loc[0, "left"] = 50
        return {"value": int(first.loc[0, "left"] + second.loc[0, "right"])}

    observation = execute_callable_current(combine, _call(left, right), adapter="pandas")

    assert observation.succeeded
    assert observation.value == {"value": 51}
    assert observation.mutated_inputs == ("args/0",)
    assert left.column("left").to_pylist() == [10, 20]

    same_table = _table()

    def mutate_first(first, second):
        first.loc[0, "x"] = 999
        return int(second.loc[0, "x"])

    independent = execute_callable_current(
        mutate_first, _call(same_table, same_table), adapter="pandas"
    )
    assert independent.value == 1
    assert independent.mutated_inputs == ("args/0",)


def test_execute_live_non_importable_callable() -> None:
    offset = 8

    def local(frame, *, multiplier=1):
        result = frame.copy()
        result["x"] = (result["x"] + offset) * multiplier
        return result

    observation = execute_callable_current(local, _call(_table(), multiplier=2), adapter="pandas")
    assert observation.succeeded
    assert observation.table is not None
    assert observation.table.column("x").to_pylist() == [18, 20]


def test_pandas_output_preserves_nan_distinct_from_null() -> None:
    import pandas as pd

    def missing_values(frame):
        return pd.DataFrame({"x": pd.Series([float("nan"), None, 1.0], dtype=object)})

    observation = execute_callable_current(missing_values, _call(_table()), adapter="pandas")
    assert observation.table is not None
    column = observation.table.column("x")
    assert column.null_count == 1
    assert column[0].as_py() != column[0].as_py()
    assert column[1].as_py() is None
    assert column[2].as_py() == 1


def test_pandas_input_materialization_is_explicit_and_defaults_to_arrow(
    transform_module: Path,
) -> None:
    target = "parity_test_transforms:pandas_input_profile"
    arrow = execute_current(
        CallableSpec(target=target, adapter="pandas"), _call(_pandas_edge_table())
    )
    native = execute_current(
        CallableSpec(target=target, adapter="pandas", pandas_input="native"),
        _call(_pandas_edge_table()),
    )

    assert arrow.succeeded
    assert native.succeeded
    assert arrow.mutated_inputs == ()
    assert native.mutated_inputs == ()
    assert arrow.value == {
        "integer_dtype": "int64[pyarrow]",
        "floating_dtype": "double[pyarrow]",
        "floating_isna": [True, False],
        "string_dtype": "string[pyarrow]",
        "boolean_dtype": "bool[pyarrow]",
        "timestamp_dtype": "timestamp[us][pyarrow]",
    }
    assert native.value is not None
    assert native.value["integer_dtype"] == "float64"
    assert native.value["floating_dtype"] == "float64"
    assert native.value["floating_isna"] == [True, True]
    assert "pyarrow" not in native.value["string_dtype"]
    assert native.value["boolean_dtype"] == "object"
    assert native.value["timestamp_dtype"].startswith("datetime64[")

    live = execute_callable_current(
        lambda frame: str(frame["integer"].dtype),
        _call(_pandas_edge_table()),
        adapter="pandas",
        pandas_input="native",
    )
    assert live.succeeded
    assert live.value == "float64"


def test_native_pandas_input_propagates_to_disposable_and_session_workers(
    transform_module: Path,
) -> None:
    spec = _isolated_spec(
        transform_module,
        "parity_test_transforms:pandas_input_profile",
        adapter="pandas",
        pandas_input="native",
    )

    disposable = execute_isolated(spec, _call(_pandas_edge_table()), timeout_seconds=5)
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        persistent = session.execute(_call(_pandas_edge_table()))

    assert disposable.succeeded
    assert persistent.succeeded
    assert disposable.value == persistent.value
    assert disposable.value is not None
    assert disposable.value["integer_dtype"] == "float64"
    assert disposable.value["floating_isna"] == [True, True]


def test_complex_invocations_round_trip_through_disposable_and_persistent_workers(
    transform_module: Path,
) -> None:
    left, right = _join_tables()
    positional_spec = _isolated_spec(
        transform_module,
        "parity_test_transforms:pandas_positional_join",
        adapter="pandas",
    )
    named_spec = _isolated_spec(
        transform_module,
        "parity_test_transforms:pandas_keyword_join",
        adapter="pandas",
    )

    disposable = execute_isolated(positional_spec, _call(left, right, 3), timeout_seconds=5)
    with IsolatedExecutionSession(named_spec, timeout_seconds=5) as session:
        persistent = session.execute(_call(left=left, right=right, amount=4))

    assert disposable.succeeded
    assert disposable.table is not None
    assert disposable.table.column("total").to_pylist() == [14, 25]
    assert disposable.mutated_inputs == ()
    assert persistent.succeeded
    assert persistent.table is not None
    assert persistent.table.column("total").to_pylist() == [15, 26]
    assert persistent.mutated_inputs == ()


def test_mutation_exception_and_json_return_are_observed(transform_module: Path) -> None:
    mutated = execute_current(
        CallableSpec(target="parity_test_transforms:pandas_mutate", adapter="pandas"),
        _call(_table()),
    )
    assert mutated.succeeded
    assert mutated.mutated_inputs == ("args/0",)

    raised = execute_current(
        CallableSpec(target="parity_test_transforms:explode", adapter="pandas"), _call(_table())
    )
    assert raised.outcome is ExecutionOutcome.RAISED
    assert raised.exception is not None
    assert raised.exception.type == "ValueError"
    assert "/private/customer" not in raised.exception.message
    assert "abc" not in raised.exception.message

    scalar = execute_current(
        CallableSpec(target="parity_test_transforms:scalar", adapter="pandas"), _call(_table())
    )
    assert scalar.succeeded
    assert scalar.has_value
    assert scalar.value == {"rows": 2, "ok": True}
    assert scalar.to_metadata()["has_value"] is True
    assert "value" not in scalar.to_metadata()


def test_failed_tabular_return_canonicalization_is_an_infrastructure_error(
    transform_module: Path,
) -> None:
    import pandas as pd

    live = execute_callable_current(
        lambda _frame: pd.DataFrame({"complex": [1 + 2j]}),
        _call(_table()),
        adapter="pandas",
    )
    current = execute_current(
        CallableSpec(
            target="parity_test_transforms:pandas_complex_output",
            adapter="pandas",
        ),
        _call(_table()),
    )
    isolated = execute_isolated(
        _isolated_spec(
            transform_module,
            "parity_test_transforms:pandas_complex_output",
            adapter="pandas",
        ),
        _call(_table()),
        timeout_seconds=5,
    )

    for observation in (live, current, isolated):
        assert observation.outcome is ExecutionOutcome.ERROR
        assert observation.exception is not None
        assert observation.exception.module == "parity.execution"
        assert observation.exception.type == "ExecutionError"
        assert observation.return_type is not None
        assert observation.return_type.startswith("pandas.")
        assert observation.return_type.endswith(".DataFrame")
        assert "1+2j" not in observation.exception.message
        assert not observation.has_value
        assert observation.table is None


def test_failed_polars_input_materialization_is_an_infrastructure_error(
    transform_module: Path,
) -> None:
    invoked = False

    def live_identity(frame):
        nonlocal invoked
        invoked = True
        return frame

    input_table = _dense_union_table()
    live = execute_callable_current(live_identity, _call(input_table), adapter="polars")
    current = execute_current(
        CallableSpec(target="parity_test_transforms:polars_add", adapter="polars"),
        _call(input_table),
    )
    isolated = execute_isolated(
        _isolated_spec(
            transform_module,
            "parity_test_transforms:polars_add",
            adapter="polars",
        ),
        _call(input_table),
        timeout_seconds=15,
    )

    assert not invoked
    for observation in (live, current, isolated):
        assert observation.outcome is ExecutionOutcome.ERROR
        assert observation.exception is not None
        assert observation.exception.module == "parity.execution"
        assert observation.exception.type == "ExecutionError"
        assert "Union" not in observation.exception.message
        assert observation.return_type is None


def test_import_time_system_exit_is_a_sanitized_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "exit_during_import.py").write_text(
        'raise SystemExit("private import detail")\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    observation = execute_current(
        CallableSpec(target="exit_during_import:transform", adapter="pandas"),
        _call(_table()),
    )

    assert observation.outcome is ExecutionOutcome.ERROR
    assert observation.exception is not None
    assert observation.exception.module == "parity.execution"
    assert observation.exception.type == "ExecutionError"
    assert "private import detail" not in observation.exception.message
    assert observation.return_type is None


@pytest.mark.parametrize(
    "value",
    [
        {1: "coerced"},
        {"nested": [{False: "coerced"}]},
    ],
)
def test_json_return_rejects_non_string_mapping_keys_recursively(value: object) -> None:
    observation = execute_callable_current(lambda _frame: value, _call(_table()), adapter="pandas")

    assert observation.outcome is ExecutionOutcome.ERROR
    assert observation.exception is not None
    assert observation.exception.type == "TypeError"
    assert observation.return_type is not None
    assert not observation.has_value


def test_json_return_rejects_cyclic_containers_as_unsupported() -> None:
    value: dict[str, object] = {}
    value["cycle"] = value

    observation = execute_callable_current(lambda _frame: value, _call(_table()), adapter="pandas")

    assert observation.outcome is ExecutionOutcome.ERROR
    assert observation.exception is not None
    assert observation.exception.type == "TypeError"
    assert observation.return_type == "builtins.dict"


def test_execute_isolated_round_trips_and_honours_workdir(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:polars_add", adapter="polars")
    spec.record_distributions = ["definitely-not-installed-parity-probe"]
    observation = execute_isolated(
        spec,
        _call(_table(), 7),
        timeout_seconds=5,
    )
    assert observation.succeeded
    assert observation.table is not None
    assert observation.table.column("x").to_pylist() == [8, 9]
    assert observation.metrics.peak_rss_bytes is None or observation.metrics.peak_rss_bytes > 0
    assert observation.runtime is not None
    recorded = {item.name: item for item in observation.runtime.distributions}
    assert recorded["definitely-not-installed-parity-probe"].status == "missing"
    assert {"numpy", "pandas", "polars", "pyarrow"}.issubset(recorded)


def test_required_distribution_fails_before_target_import(tmp_path: Path) -> None:
    imported = tmp_path / "target-imported.txt"
    (tmp_path / "must_not_import.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(imported)!r}).write_text('imported', encoding='utf-8')\n"
        "def transform(frame):\n"
        "    return frame\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="must_not_import:transform",
        adapter="arrow",
        workdir=tmp_path,
        required_distributions={"definitely-missing-parity-contract": ">=1"},
    )

    observation = execute_current(spec, _call(_table()))

    assert observation.outcome is ExecutionOutcome.ERROR
    assert observation.exception is not None
    assert observation.exception.type == "RuntimeContractError"
    assert observation.exception.message == (
        "worker runtime requirements not satisfied: "
        "distributions.definitely-missing-parity-contract.missing"
    )
    assert observation.runtime is not None
    assert imported.exists() is False


def test_disposable_worker_does_not_require_target_side_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported = tmp_path / "disposable-target-imported.txt"
    (tmp_path / "disposable_must_not_import.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(imported)!r}).write_text('imported', encoding='utf-8')\n"
        "def transform(frame):\n"
        "    return frame\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="disposable_must_not_import:transform",
        adapter="arrow",
        workdir=tmp_path,
    )
    monkeypatch.setattr("parity.execution.__version__", "999.0.0")

    observation = execute_isolated(spec, _call(_table()), timeout_seconds=5)

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.exception is None
    assert imported.exists() is True


def test_isolated_workers_apply_relative_workdir_once(
    transform_module: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(transform_module.parent)
    spec = CallableSpec(
        target="parity_test_transforms:scalar",
        adapter="pandas",
        workdir=Path(transform_module.name),
    )

    disposable = execute_isolated(spec, _call(_table()), timeout_seconds=5)
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        persistent = session.execute(_call(_table()))

    assert disposable.succeeded
    assert persistent.succeeded
    assert disposable.value == persistent.value == {"rows": 2, "ok": True}


def test_isolated_session_matches_fresh_worker_observation(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:polars_add", adapter="polars")
    fresh = execute_isolated(spec, _call(_table(), 7), timeout_seconds=5)
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        persistent = session.execute(_call(_table(), 7))

    assert persistent.outcome is fresh.outcome is ExecutionOutcome.RETURNED
    assert persistent.table is not None
    assert fresh.table is not None
    assert persistent.table.equals(fresh.table)
    assert persistent.mutated_inputs == fresh.mutated_inputs
    assert persistent.return_type == fresh.return_type


def test_isolated_session_preserves_module_state_but_refreshes_each_input(
    transform_module: Path,
) -> None:
    spec = _isolated_spec(
        transform_module, "parity_test_transforms:stateful_and_mutate", adapter="pandas"
    )
    session = IsolatedExecutionSession(spec, timeout_seconds=5)
    with session:
        first = session.execute(_call(_table()))
        second = session.execute(_call(pa.table({"x": [41], "name": ["fresh"]})))

    assert first.succeeded
    assert first.mutated_inputs == ("args/0",)
    assert second.succeeded
    assert second.mutated_inputs == ("args/0",)
    assert first.value == {"call": 1, "input": 1}
    assert second.value == {"call": 2, "input": 41}
    assert session.closed
    unavailable = session.execute(_call(_table()))
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionClosedError"


def test_isolated_session_crash_fails_closed(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:hard_crash", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        crashed = session.execute(_call(_table()))
        unavailable = session.execute(_call(_table()))

    assert crashed.outcome is ExecutionOutcome.CRASHED
    assert crashed.exception is not None
    assert crashed.exception.type == "WorkerSessionError"
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


def test_isolated_session_timeout_fails_closed(transform_module: Path) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:wait", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=0.2) as session:
        timed_out = session.execute(_call(_table(), 30))
        unavailable = session.execute(_call(_table()))

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

    spec = _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        assert session.preflight_runtime().succeeded
        monkeypatch.setattr("parity.execution.json.loads", malformed)
        malformed_response = session.execute(_call(_table()))
        unavailable = session.execute(_call(_table()))

    assert malformed_response.outcome is ExecutionOutcome.CRASHED
    assert malformed_response.exception is not None
    assert malformed_response.exception.type == "WorkerProtocolError"
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


def test_isolated_session_outputless_execute_response_fails_closed(
    transform_module: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_loads = __import__("parity.execution", fromlist=["json"]).json.loads

    def outputless(payload: str):
        parsed = original_loads(payload)
        if isinstance(parsed, dict) and "outcome" in parsed:
            parsed["output"] = None
            parsed["return_type"] = None
        return parsed

    spec = _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        assert session.preflight_runtime().succeeded
        monkeypatch.setattr("parity.execution.json.loads", outputless)
        malformed = session.execute(_call(_table()))
        unavailable = session.execute(_call(_table()))

    assert malformed.outcome is ExecutionOutcome.CRASHED
    assert malformed.exception is not None
    assert malformed.exception.type == "WorkerProtocolError"
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


def test_isolated_session_allows_outputless_provenance_response(
    transform_module: Path,
) -> None:
    spec = _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        provenance = session.inspect_runtime()

    assert provenance.succeeded
    assert provenance.runtime is not None
    assert provenance.table is None
    assert not provenance.has_value


def test_isolated_session_malformed_runtime_fails_closed(
    transform_module: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_loads = __import__("parity.execution", fromlist=["json"]).json.loads

    def malformed_runtime(payload: str):
        parsed = original_loads(payload)
        if isinstance(parsed, dict) and "outcome" in parsed:
            parsed["runtime"] = {"python_version": "/private/unsafe"}
        return parsed

    spec = _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        assert session.preflight_runtime().succeeded
        monkeypatch.setattr("parity.execution.json.loads", malformed_runtime)
        malformed = session.execute(_call(_table()))
        unavailable = session.execute(_call(_table()))

    assert malformed.outcome is ExecutionOutcome.CRASHED
    assert malformed.exception is not None
    assert malformed.exception.type == "WorkerProtocolError"
    assert unavailable.outcome is ExecutionOutcome.CRASHED
    assert unavailable.exception is not None
    assert unavailable.exception.type == "WorkerSessionUnavailableError"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mutated_inputs", ["customer-secret"]),
        ("mutated_input", True),
    ],
)
def test_isolated_session_invalid_mutation_metadata_fails_closed(
    transform_module: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    original_loads = __import__("parity.execution", fromlist=["json"]).json.loads

    def invalid_mutation(payload: str):
        parsed = original_loads(payload)
        if isinstance(parsed, dict) and "outcome" in parsed:
            parsed[field] = value
        return parsed

    spec = _isolated_spec(transform_module, "parity_test_transforms:scalar", adapter="pandas")
    with IsolatedExecutionSession(spec, timeout_seconds=5) as session:
        assert session.preflight_runtime().succeeded
        monkeypatch.setattr("parity.execution.json.loads", invalid_mutation)
        malformed = session.execute(_call(_table()))
        unavailable = session.execute(_call(_table()))

    assert malformed.outcome is ExecutionOutcome.CRASHED
    assert malformed.exception is not None
    assert malformed.exception.type == "WorkerProtocolError"
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
        observation = session.execute(_call(_table(), str(pid_file)))
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
        _call(_table(), str(pid_file)),
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
        _call(_table()),
    )
    assert isolated.succeeded
    assert isolated.value == {"rows": 2, "ok": True}
    with pytest.raises(ExecutionError, match="isolated"):
        execute_current(
            CallableSpec(target="parity_test_transforms:scalar", python=Path("/different/python")),
            _call(_table()),
        )


def test_redaction_removes_paths_and_secret_assignments() -> None:
    redacted = redact_text("failed at /srv/customer/a.csv with API_KEY=hunter2")
    assert "/srv/customer" not in redacted
    assert "hunter2" not in redacted
    assert "<path>" in redacted
    assert "<redacted>" in redacted
