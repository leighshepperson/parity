"""Soak the persistent C++ command adapter with returns and domain exceptions."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path

import pyarrow as pa
from generator import regression_lot_size, regression_reference_rejects

from parity.config import load_config
from parity.execution import ExecutionOutcome, IsolatedExecutionSession, Observation


class SoakError(RuntimeError):
    """The adapter soak observed an incorrect or uncertain outcome."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=2000)
    arguments = parser.parse_args()
    if arguments.calls < 1:
        parser.error("--calls must be positive")
    return arguments


def _first(values: object) -> Mapping[str, pa.Table]:
    bundle = next(iter(values))  # type: ignore[arg-type]
    if not isinstance(bundle, Mapping):
        raise SoakError("regression generator did not return a named input bundle")
    return bundle


def _assert_pair(
    reference: Observation,
    candidate: Observation,
    expected: ExecutionOutcome,
    index: int,
) -> None:
    if reference.outcome is not expected or candidate.outcome is not expected:
        raise SoakError(
            f"call {index} expected {expected.value}, received "
            f"{reference.outcome.value}/{candidate.outcome.value}"
        )
    if expected is ExecutionOutcome.RETURNED:
        if (
            reference.table is None
            or candidate.table is None
            or not reference.table.equals(candidate.table)
        ):
            raise SoakError(f"call {index} returned different tables")
        return
    if reference.exception is None or candidate.exception is None:
        raise SoakError(f"call {index} omitted exception metadata")
    if reference.exception.fingerprint != candidate.exception.fingerprint:
        raise SoakError(f"call {index} returned different exception semantics")


def main() -> None:
    arguments = _arguments()
    root = Path(__file__).resolve().parent
    config = load_config(root / "parity.toml")
    control = next(case for case in config.cases if case.name == "correct-port")
    returning = _first(regression_lot_size())
    raising = _first(regression_reference_rejects())
    counts = {ExecutionOutcome.RETURNED: 0, ExecutionOutcome.RAISED: 0}

    with (
        IsolatedExecutionSession(control.reference, timeout_seconds=15) as reference_session,
        IsolatedExecutionSession(control.candidate, timeout_seconds=15) as candidate_session,
    ):
        for index in range(arguments.calls):
            expected = ExecutionOutcome.RETURNED if index % 2 == 0 else ExecutionOutcome.RAISED
            bundle = returning if expected is ExecutionOutcome.RETURNED else raising
            reference = reference_session.execute(bundle)
            candidate = candidate_session.execute(bundle)
            _assert_pair(reference, candidate, expected, index)
            counts[expected] += 1

    print(
        "PASS persistent adapter soak "
        f"({arguments.calls} paired calls: {counts[ExecutionOutcome.RETURNED]} returned, "
        f"{counts[ExecutionOutcome.RAISED]} raised)"
    )


if __name__ == "__main__":
    try:
        main()
    except SoakError as error:
        raise SystemExit(f"FAIL {error}") from None
