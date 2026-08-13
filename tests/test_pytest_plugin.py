from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from parity.models import (
    CaseResult,
    ExampleResult,
    Mismatch,
    MismatchKind,
    Status,
    SuiteResult,
)
from parity.pytest_plugin import ParityAssertions

pytest_plugins = ["pytester"]


def suite(status: Status = Status.PASSED) -> SuiteResult:
    failures = []
    if status is not Status.PASSED:
        failures = [
            ExampleResult(
                source="generated",
                status=status,
                mismatches=[
                    Mismatch(
                        kind=MismatchKind.VALUE,
                        message="different total",
                        path="rows[0].total",
                    )
                ],
                artifact=Path(".parity/orders/witness"),
            )
        ]
    return SuiteResult(
        status=status,
        cases=[
            CaseResult(
                name="orders",
                status=status,
                examples_run=3,
                failures=failures,
            )
        ],
    )


def test_assert_passed_returns_result() -> None:
    result = suite()
    assert ParityAssertions.assert_passed(result) is result


def test_assert_passed_has_compact_actionable_failure() -> None:
    with pytest.raises(pytest.fail.Exception) as error:
        ParityAssertions.assert_passed(suite(Status.FAILED))
    message = str(error.value)
    assert "semantic parity check failed" in message
    assert "rows[0].total" in message
    assert ".parity/orders/witness" in message


def test_check_forwards_configuration_and_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, Any] = {}

    def fake_check(config: str | Path, *, cases: set[str] | None = None) -> SuiteResult:
        received.update(config=config, cases=cases)
        return suite()

    monkeypatch.setattr("parity.pytest_plugin.parity_api.check", fake_check)
    assertions = ParityAssertions("default.toml", {"orders"})
    result = assertions.check()
    assert result.passed
    assert received == {"config": Path("default.toml"), "cases": {"orders"}}


def test_verify_forwards_public_api_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, Any] = {}

    def fake_verify(reference: object, candidate: object, **kwargs: Any) -> SuiteResult:
        received.update(reference=reference, candidate=candidate, **kwargs)
        return suite()

    monkeypatch.setattr("parity.pytest_plugin.parity_api.verify", fake_verify)
    reference, candidate = object(), object()
    ParityAssertions("unused.toml").verify(reference, candidate, artifact_dir="evidence")
    assert received == {
        "reference": reference,
        "candidate": candidate,
        "artifact_dir": "evidence",
    }


def test_plugin_cli_and_marker_override(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parity(config="marked.toml", cases=["a", "b"])
        def test_values(parity):
            assert str(parity.config) == "marked.toml"
            assert parity.cases == {"a", "b"}

        def test_cli(parity):
            assert str(parity.config) == "cli.toml"
            assert parity.cases == {"selected"}
        """
    )
    result = pytester.runpytest(
        "--parity-config",
        "cli.toml",
        "--parity-case",
        "selected",
    )
    result.assert_outcomes(passed=2)


def test_parity_case_requires_exactly_one_selection(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_selected(parity_case):
            assert parity_case
        """
    )
    result = pytester.runpytest()
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*parity_case requires exactly one --parity-case option*"])
