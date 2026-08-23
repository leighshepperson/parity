from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pyarrow as pa
import pytest

from parity.execution import ExecutionOutcome, IsolatedExecutionSession, execute_isolated
from parity.invocation import FrameSequence, Invocation
from parity.models import CallableSpec
from parity.target_adapter import AdapterError, require_executable

SOURCE_ROOT = Path(__file__).parents[1] / "src"


def _write_adapter(
    tmp_path: Path,
    execute_body: str,
    *,
    inspect_body: str = "return None",
    return_type: str | None = None,
) -> Path:
    script = tmp_path / "target_adapter.py"
    source = textwrap.dedent(
        f"""
        from __future__ import annotations

        import os
        import sys
        from pathlib import Path

        import pyarrow as pa

        from parity.target_adapter import (
            AdapterError,
            CommandAdapter,
            Return,
            RuntimeInfo,
            TargetRaised,
            require_executable,
        )


        def inspect() -> None:
        __INSPECT_BODY__


        def execute(*args, **kwargs):
        __EXECUTE_BODY__


        CommandAdapter(
            runtime=RuntimeInfo("legacy-runtime", "7.4", distributions=("pyarrow",)),
            execute=execute,
            inspect=inspect,
            return_type={return_type!r},
        ).serve()
        """
    ).lstrip()
    source = source.replace(
        "__INSPECT_BODY__",
        textwrap.indent(textwrap.dedent(inspect_body).strip(), "    "),
    ).replace(
        "__EXECUTE_BODY__",
        textwrap.indent(textwrap.dedent(execute_body).strip(), "    "),
    )
    script.write_text(source, encoding="utf-8")
    script.chmod(0o700)
    return script


def _spec(
    script: Path,
    *,
    environment: dict[str, str] | None = None,
    arguments: tuple[str, ...] = (),
) -> CallableSpec:
    return CallableSpec(
        command=[sys.executable, str(script), *arguments],
        workdir=script.parent,
        environment={"PYTHONPATH": str(SOURCE_ROOT), **(environment or {})},
        record_distributions=["pyarrow"],
    )


def _mode_table(mode: str, value: int = 1) -> pa.Table:
    return pa.table({"mode": [mode], "value": [value]})


def test_command_adapter_returns_arrow_and_json(tmp_path: Path) -> None:
    script = _write_adapter(
        tmp_path,
        """
        table = args[0]
        mode = table.column("mode")[0].as_py()
        value = table.column("value")[0].as_py()
        if mode == "arrow":
            return Return(
                pa.table({"result": [value + 1]}),
                return_type="legacy.ArrowResult",
                mutated_inputs=("args/0",),
            )
        return {"result": value + 2, "labels": ["json", "canonical"]}
        """,
        return_type="legacy.JsonResult",
    )

    arrow = execute_isolated(
        _spec(script), Invocation(args=(_mode_table("arrow", 40),)), timeout_seconds=5
    )
    json_value = execute_isolated(
        _spec(script), Invocation(args=(_mode_table("json", 40),)), timeout_seconds=5
    )

    assert arrow.outcome is ExecutionOutcome.RETURNED
    assert arrow.table == pa.table({"result": [41]})
    assert arrow.value is None
    assert not arrow.has_value
    assert arrow.return_type == "legacy.ArrowResult"
    assert arrow.mutated_inputs == ("args/0",)

    assert json_value.outcome is ExecutionOutcome.RETURNED
    assert json_value.table is None
    assert json_value.has_value
    assert json_value.value == {"result": 42, "labels": ["json", "canonical"]}
    assert json_value.return_type == "legacy.JsonResult"


@pytest.mark.parametrize(
    ("invocation", "expected"),
    [
        (
            Invocation(args=(pa.table({"value": [1]}), 9), kwargs={"scale": 2}),
            {
                "positional_tables": [1],
                "positional_scalars": [9],
                "keyword_tables": {},
                "keyword_scalars": {"scale": 2},
            },
        ),
        (
            Invocation(
                args=(pa.table({"value": [2]}), pa.table({"value": [3]}), 10),
                kwargs={"scale": 4},
            ),
            {
                "positional_tables": [2, 3],
                "positional_scalars": [10],
                "keyword_tables": {},
                "keyword_scalars": {"scale": 4},
            },
        ),
        (
            Invocation(
                kwargs={
                    "left": pa.table({"value": [4]}),
                    "right": pa.table({"value": [5]}),
                    "scale": 3,
                }
            ),
            {
                "positional_tables": [],
                "positional_scalars": [],
                "keyword_tables": {"left": 4, "right": 5},
                "keyword_scalars": {"scale": 3},
            },
        ),
    ],
)
def test_command_adapter_preserves_complete_invocation(
    tmp_path: Path,
    invocation: Invocation,
    expected: dict[str, object],
) -> None:
    script = _write_adapter(
        tmp_path,
        """
        return {
            "positional_tables": [
                value.column("value")[0].as_py()
                for value in args
                if isinstance(value, pa.Table)
            ],
            "positional_scalars": [
                value for value in args if not isinstance(value, pa.Table)
            ],
            "keyword_tables": {
                name: value.column("value")[0].as_py()
                for name, value in kwargs.items()
                if isinstance(value, pa.Table)
            },
            "keyword_scalars": {
                name: value
                for name, value in kwargs.items()
                if not isinstance(value, pa.Table)
            },
        }
        """,
    )

    observation = execute_isolated(
        _spec(script),
        invocation,
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.has_value
    assert observation.value == expected


def test_command_adapter_preserves_frame_sequence_and_expanded_varargs(tmp_path: Path) -> None:
    script = _write_adapter(
        tmp_path,
        """
        operation, *frames = args
        batches = kwargs["batches"]
        return {
            "operation": operation,
            "varargs": [table.column("value")[0].as_py() for table in frames],
            "batch_container": type(batches).__name__,
            "batches": [table.column("value")[0].as_py() for table in batches],
            "descending": kwargs["descending"],
        }
        """,
    )
    invocation = Invocation(
        args=("sum", pa.table({"value": [1]}), pa.table({"value": [2]})),
        kwargs={
            "batches": FrameSequence(
                (pa.table({"value": [3]}), pa.table({"value": [4]})),
                "tuple",
            ),
            "descending": False,
        },
    )

    observation = execute_isolated(_spec(script), invocation, timeout_seconds=5)

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.value == {
        "operation": "sum",
        "varargs": [1, 2],
        "batch_container": "tuple",
        "batches": [3, 4],
        "descending": False,
    }


def test_command_adapter_distinguishes_target_raise_and_adapter_errors(
    tmp_path: Path,
) -> None:
    script = _write_adapter(
        tmp_path,
        """
        mode = args[0].column("mode")[0].as_py()
        if mode == "raised":
            raise TargetRaised(
                "record 7849231 was rejected",
                module="legacy.domain",
                exception_type="DomainRejected",
                details={"error_codes": ["expired"]},
                mutated_inputs=("args/0",),
            )
        if mode == "adapter-error":
            raise AdapterError("invalid_output", "legacy output was invalid")
        raise RuntimeError(
            "TOKEN=top-secret at /tmp/private-customer/record-7849231"
        )
        """,
    )

    raised = execute_isolated(
        _spec(script), Invocation(args=(_mode_table("raised"),)), timeout_seconds=5
    )
    adapter_error = execute_isolated(
        _spec(script), Invocation(args=(_mode_table("adapter-error"),)), timeout_seconds=5
    )
    unexpected = execute_isolated(
        _spec(script), Invocation(args=(_mode_table("unexpected"),)), timeout_seconds=5
    )

    assert raised.outcome is ExecutionOutcome.RAISED
    assert raised.exception is not None
    assert raised.exception.module == "legacy.domain"
    assert raised.exception.type == "DomainRejected"
    assert raised.exception.details == {"error_codes": ["expired"]}
    assert raised.mutated_inputs == ("args/0",)

    assert adapter_error.outcome is ExecutionOutcome.ERROR
    assert adapter_error.exception is not None
    assert adapter_error.exception.type == "AdapterError"
    assert adapter_error.exception.details == {"error_codes": ["invalid_output"]}

    assert unexpected.outcome is ExecutionOutcome.ERROR
    assert unexpected.exception is not None
    unexpected_metadata = str(unexpected.exception.to_dict())
    assert "top-secret" not in unexpected_metadata
    assert "private-customer" not in unexpected_metadata
    assert "7849231" not in unexpected_metadata


def test_runtime_does_not_inspect_and_inspect_can_require_executable(tmp_path: Path) -> None:
    marker = tmp_path / "inspected.txt"
    script = _write_adapter(
        tmp_path,
        "return {'executed': True}",
        inspect_body="""
        executable = require_executable(Path(__file__))
        assert executable.is_absolute()
        marker = Path(os.environ["PARITY_TEST_INSPECT_MARKER"])
        count = int(marker.read_text(encoding="utf-8")) if marker.exists() else 0
        marker.write_text(str(count + 1), encoding="utf-8")
        """,
    )

    with IsolatedExecutionSession(
        _spec(script, environment={"PARITY_TEST_INSPECT_MARKER": str(marker)}),
        timeout_seconds=5,
    ) as session:
        runtime = session.inspect_runtime()
        assert runtime.outcome is ExecutionOutcome.RETURNED
        assert not marker.exists()

        inspected = session.inspect_endpoint()
        assert inspected.outcome is ExecutionOutcome.RETURNED
        assert marker.read_text(encoding="utf-8") == "1"


def test_require_executable_rejects_a_missing_program(tmp_path: Path) -> None:
    program = tmp_path / "program"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o700)

    assert require_executable(program) == program.resolve()

    with pytest.raises(AdapterError, match="executable"):
        require_executable(tmp_path / "missing-program")


def test_require_executable_accepts_a_normal_executable_symlink(tmp_path: Path) -> None:
    program = tmp_path / "program"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o700)
    linked = tmp_path / "program-link"
    try:
        linked.symlink_to(program)
    except OSError:
        pytest.skip("filesystem does not permit executable symlinks")

    assert require_executable(linked) == program.resolve()


def test_command_adapter_default_serve_allows_configured_arguments(tmp_path: Path) -> None:
    script = _write_adapter(tmp_path, "return {'compat': True}")

    observation = execute_isolated(
        _spec(script, arguments=("--compat",)),
        Invocation(args=(pa.table({"value": [1]}),)),
        timeout_seconds=5,
    )

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.value == {"compat": True}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinked temp roots need POSIX semantics")
def test_command_adapter_accepts_a_symlinked_session_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _write_adapter(tmp_path, "return args[0]")
    real_temporary = tmp_path / "real-temporary"
    real_temporary.mkdir()
    linked_temporary = tmp_path / "linked-temporary"
    try:
        linked_temporary.symlink_to(real_temporary, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not permit directory symlinks")
    monkeypatch.setattr(tempfile, "tempdir", str(linked_temporary))
    table = pa.table({"value": [1, 2]})

    observation = execute_isolated(_spec(script), Invocation(args=(table,)), timeout_seconds=5)

    assert observation.outcome is ExecutionOutcome.RETURNED
    assert observation.table == table


def test_command_adapter_session_preserves_state_and_runtime_identity(tmp_path: Path) -> None:
    script = _write_adapter(
        tmp_path,
        """
        global call_count
        call_count += 1
        return {"call_count": call_count}
        """,
    )
    source = script.read_text(encoding="utf-8")
    script.write_text(
        source.replace(
            "def execute(*args, **kwargs):",
            "call_count = 0\n\n\ndef execute(*args, **kwargs):",
        ),
        encoding="utf-8",
    )

    with IsolatedExecutionSession(_spec(script), timeout_seconds=5) as session:
        preflight = session.preflight_runtime()
        first = session.execute(Invocation(args=(pa.table({"value": [1]}),)))
        second = session.execute(Invocation(args=(pa.table({"value": [2]}),)))

    assert preflight.outcome is ExecutionOutcome.RETURNED
    assert preflight.runtime is not None
    assert preflight.runtime.executor == "command"
    assert preflight.runtime.runtime_name == "legacy-runtime"
    assert preflight.runtime.runtime_version == "7.4"
    assert preflight.runtime.parity_version is not None
    assert any(
        distribution.name == "pyarrow" and distribution.status == "installed"
        for distribution in preflight.runtime.distributions
    )

    assert first.outcome is ExecutionOutcome.RETURNED
    assert first.value == {"call_count": 1}
    assert second.outcome is ExecutionOutcome.RETURNED
    assert second.value == {"call_count": 2}
    assert first.runtime == preflight.runtime == second.runtime
