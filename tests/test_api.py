from __future__ import annotations

import pytest

import parity.api as api
from parity.api import check
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseResult,
    ColumnSchema,
    FrameSchema,
    ParityConfig,
    Status,
    SuiteResult,
)


def test_check_rejects_explicit_empty_selection_before_loading_config() -> None:
    with pytest.raises(ValueError, match="cases must contain at least one case name"):
        check("missing.toml", cases=set())


def test_check_applies_parallel_execution_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ParityConfig(
        cases=[
            CaseConfig(
                name="orders",
                reference=CallableSpec(target="project:legacy"),
                candidate=CallableSpec(target="project:replacement"),
                input_schema=FrameSchema(columns=[ColumnSchema(name="order_id", dtype="integer")]),
            )
        ]
    )
    observed: list[tuple[int, int | None, set[str] | None]] = []
    monkeypatch.setattr(api, "load_config", lambda _path: config)

    def run_suite(selected: ParityConfig, *, selected_cases: set[str] | None = None) -> SuiteResult:
        observed.append((selected.jobs, selected.native_threads, selected_cases))
        return SuiteResult(
            status=Status.PASSED,
            cases=[CaseResult(name="orders", status=Status.PASSED)],
        )

    monkeypatch.setattr("parity.engine.run_suite", run_suite)

    result = check("parity.toml", cases={"orders"}, jobs=4, native_threads=1)

    assert result.status is Status.PASSED
    assert observed == [(4, 1, {"orders"})]
