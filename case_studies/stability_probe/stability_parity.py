"""Controlled hidden-state targets for the same-input stability study."""

from __future__ import annotations

import pyarrow as pa

_reference_calls = 0
_candidate_calls = 0


def reference(frame: pa.Table) -> pa.Table:
    """Return a value that changes with hidden reference-process state."""

    global _reference_calls
    _reference_calls += 1
    return frame.append_column("observation", pa.array([_reference_calls] * frame.num_rows))


def candidate(frame: pa.Table) -> pa.Table:
    """Mirror the same hidden-state bug in the candidate process."""

    global _candidate_calls
    _candidate_calls += 1
    return frame.append_column("observation", pa.array([_candidate_calls] * frame.num_rows))
