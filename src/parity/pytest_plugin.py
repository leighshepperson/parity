"""Pytest integration for treating semantic parity as an ordinary test assertion."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from parity import api as parity_api
from parity.models import SuiteResult


def _failure_message(result: SuiteResult) -> str:
    lines = ["semantic parity check failed"]
    for case in result.cases:
        if case.status.value == "passed":
            continue
        lines.append(
            f"- {case.name}: {case.status.value}; "
            f"{case.examples_run} example(s), {len(case.failures)} failure(s)"
        )
        for example in case.failures[:3]:
            for mismatch in example.mismatches[:3]:
                path = f" at {mismatch.path}" if mismatch.path else ""
                lines.append(f"  - {mismatch.kind.value}{path}: {mismatch.message}")
            if example.artifact is not None:
                lines.append(f"  - replay artifact: {example.artifact}")
    return "\n".join(lines)


class ParityAssertions:
    """Assertion-oriented facade exposed by the ``parity`` fixture."""

    def __init__(self, config: str | Path, cases: set[str] | None = None) -> None:
        self.config = Path(config)
        self.cases = cases

    def check(
        self,
        config: str | Path | None = None,
        *,
        cases: set[str] | None = None,
    ) -> SuiteResult:
        """Run a configured suite and fail the current pytest test on mismatch."""

        selected = cases if cases is not None else self.cases
        if selected is not None and not selected:
            pytest.fail(
                "parity case selection must contain at least one case name",
                pytrace=False,
            )
        result = parity_api.check(config or self.config, cases=selected)
        return self.assert_passed(result)

    def verify(
        self,
        reference: Callable[..., Any],
        candidate: Callable[..., Any],
        **kwargs: Any,
    ) -> SuiteResult:
        """Verify live functions and fail the current pytest test on mismatch."""

        result = parity_api.verify(reference, candidate, **kwargs)
        return self.assert_passed(result)

    @staticmethod
    def assert_passed(result: SuiteResult) -> SuiteResult:
        """Assert an already-computed result while preserving it for inspection."""

        if not result.passed:
            pytest.fail(_failure_message(result), pytrace=False)
        return result


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("parity", "semantic migration verification")
    group.addoption(
        "--parity-config",
        action="store",
        default="parity.toml",
        metavar="PATH",
        help="configuration used by the parity and parity_case fixtures",
    )
    group.addoption(
        "--parity-case",
        action="append",
        default=[],
        metavar="NAME",
        help="select a configured case; repeat to select multiple cases",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "parity(config='parity.toml', cases=[]): override Parity fixture configuration",
    )


@pytest.fixture
def parity(request: pytest.FixtureRequest) -> ParityAssertions:
    """Return assertions configured by CLI options or ``@pytest.mark.parity``."""

    config_path: str | Path = request.config.getoption("--parity-config")
    selected = set(request.config.getoption("--parity-case")) or None
    marker = request.node.get_closest_marker("parity")
    if marker is not None:
        if marker.args:
            raise pytest.UsageError("@pytest.mark.parity accepts keyword arguments only")
        unknown = set(marker.kwargs) - {"config", "cases"}
        if unknown:
            raise pytest.UsageError(f"unknown @pytest.mark.parity arguments: {sorted(unknown)}")
        config_path = marker.kwargs.get("config", config_path)
        marker_cases = marker.kwargs.get("cases")
        if marker_cases is not None:
            if isinstance(marker_cases, str):
                marker_cases = [marker_cases]
            selected = set(marker_cases)
            if not selected:
                raise pytest.UsageError(
                    "@pytest.mark.parity cases must contain at least one case name"
                )
    return ParityAssertions(config_path, selected)


@pytest.fixture
def parity_case(request: pytest.FixtureRequest) -> str:
    """Return the case selected with ``--parity-case`` for explicit parametrization.

    This fixture is intentionally not auto-parametrized from a configuration file,
    because reading project files during pytest collection makes unrelated tests
    brittle. Use ``@pytest.mark.parametrize`` when a test needs one invocation per
    case; the main ``parity`` fixture can run all configured cases in one assertion.
    """

    selected: list[str] = request.config.getoption("--parity-case")
    if len(selected) != 1:
        raise pytest.UsageError(
            "parity_case requires exactly one --parity-case option; "
            "use the parity fixture to run zero or multiple selections"
        )
    return selected[0]


__all__ = ["ParityAssertions", "parity", "parity_case"]
