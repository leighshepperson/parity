"""Cross-library semantic comparison with explicit equivalence policy."""

from __future__ import annotations

import datetime as dt
import decimal
import fractions
import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np

from parity.canonical import (
    CanonicalFrame,
    CanonicalSeries,
    ExceptionInfo,
    canonicalize,
    is_nan,
    is_null,
    json_safe,
)
from parity.models import ComparisonPolicy, Mismatch, MismatchKind


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Structured result for callers that prefer an equivalence flag."""

    mismatches: tuple[Mismatch, ...]

    @property
    def equivalent(self) -> bool:
        return not self.mismatches


_SIGNATURE_VERSION = "ms1"
_SIGNATURE_KIND_PRIORITY = {
    # Prefer contract-level symptoms over downstream cell differences. A
    # single execution can emit several mismatches; selecting one conservative
    # primary symptom avoids presenting every secondary effect as a finding.
    MismatchKind.EXCEPTION: 0,
    MismatchKind.MUTATION: 1,
    MismatchKind.SCHEMA: 2,
    MismatchKind.COLUMN: 3,
    MismatchKind.SHAPE: 4,
    MismatchKind.DTYPE: 5,
    MismatchKind.ROW: 6,
    MismatchKind.VALUE: 7,
    MismatchKind.PERFORMANCE: 8,
}
_SIGNATURE_MESSAGE_CODES = {
    "null values are not equivalent": "null-null",
    "null differs from a value": "null-value",
    "one result is tabular and the other is not": "frame-type",
    "one result is a series and the other is not": "series-type",
    "mapping differs from non-mapping": "mapping-type",
    "mapping keys differ": "mapping-keys",
    "sequence differs from non-sequence": "sequence-type",
    "sequence lengths differ": "sequence-length",
    "set differs from non-set": "set-type",
    "set members differ": "set-members",
    "datetime values differ": "datetime-value",
    "duration values differ": "duration-value",
    "numeric values differ beyond tolerance": "numeric-value",
    "boolean differs from a non-boolean value": "boolean-type",
    "values differ": "value",
    "one implementation raised and the other returned": "raise-return",
    "raised exceptions differ": "exception-contract",
    "column dtype differs": "column-dtype",
    "dtype differs": "dtype",
    "series names differ": "series-name",
    "series lengths differ": "series-length",
    "column names are ambiguous under the selected name policy": "ambiguous-columns",
    "column sets differ": "column-set",
    "column order differs": "column-order",
    "row counts differ": "row-count",
    "reference row has no equivalent candidate row": "missing-candidate-row",
    "candidate contains an unmatched row": "unexpected-candidate-row",
    "configured row key columns are unavailable": "row-key-columns",
    "row keys are not unique": "duplicate-row-key",
    "row key is not alignable under the comparison policy": "nonreflexive-row-key",
    "reference row key has no candidate row": "missing-candidate-key",
    "candidate row key has no reference row": "unexpected-candidate-key",
    "row key contains a non-scalar value": "unsupported-row-key",
    "input mutation behaviour differs": "input-mutation",
}
_INDEXED_PATH = re.compile(r"\[(?:-?\d+|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\]")


def _normalized_signature_path(path: str | None) -> str:
    """Retain structural path shape without persisting user-defined names."""

    if not path:
        return "$"
    normalized = _INDEXED_PATH.sub("[*]", path)
    if normalized.startswith("$inputs/"):
        return "$inputs/<input>"
    prefix, separator, _field = normalized.partition(".")
    if separator:
        return f"{prefix}.<field>"
    return normalized


def _signature_component(mismatch: Mismatch) -> tuple[int, str, str, str]:
    return (
        _SIGNATURE_KIND_PRIORITY[mismatch.kind],
        mismatch.kind.value,
        _SIGNATURE_MESSAGE_CODES.get(mismatch.message, f"{mismatch.kind.value}-other"),
        _normalized_signature_path(mismatch.path),
    )


def _stable_mapping_key(value: Any) -> tuple[str, str, str]:
    """Order heterogeneous mapping keys independently of Python's hash seed."""

    try:
        encoded = json.dumps(
            json_safe(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - json_safe is defensive
        encoded = repr(value)
    return type(value).__module__, type(value).__qualname__, encoded


def mismatch_signature(mismatches: Sequence[Mismatch]) -> str:
    """Return a stable, data-free signature for an observable mismatch shape.

    A signature identifies one conservative primary symptom, not a root cause
    or a claim that two signatures are two separate bugs. Compared values,
    verbose details, exception text and witness-dependent row indices are never
    included. The prefix versions the canonicalization contract.
    """

    if not mismatches:
        raise ValueError("mismatch_signature requires at least one mismatch")
    primary = min(_signature_component(mismatch) for mismatch in mismatches)
    encoded = json.dumps(
        {"version": _SIGNATURE_VERSION, "primary": primary},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{_SIGNATURE_VERSION}:{hashlib.sha256(encoded).hexdigest()}"


def _mismatch(
    kind: MismatchKind,
    message: str,
    path: str,
    reference: Any = None,
    candidate: Any = None,
    **details: Any,
) -> Mismatch:
    return Mismatch(
        kind=kind,
        message=message,
        path=path,
        reference=json_safe(reference),
        candidate=json_safe(candidate),
        details={key: json_safe(value) for key, value in details.items()},
    )


def _name_key(name: str, policy: ComparisonPolicy) -> str:
    return name if policy.names == "strict" else name.casefold()


def _datetime_ns(value: Any) -> int | None:
    if isinstance(value, np.datetime64):
        normalized = value.astype("datetime64[ns]").astype(np.int64)
        return int(cast(int, normalized))
    if isinstance(value, dt.datetime):
        # pandas.Timestamp subclasses datetime but can carry nanoseconds that
        # Python's datetime fields cannot represent.
        try:
            exact_value = getattr(value, "value", None)
        except (OverflowError, ValueError):
            exact_value = None
        if isinstance(exact_value, (int, np.integer)):
            return int(exact_value)
        datetime_value = value
        if datetime_value.tzinfo is not None:
            datetime_value = datetime_value.astimezone(dt.UTC).replace(tzinfo=None)
        epoch = dt.datetime(1970, 1, 1)
        delta = datetime_value - epoch
        # Integer arithmetic avoids losing sub-microsecond precision after
        # multiplying a large epoch float.
        return (
            delta.days * 86_400_000_000_000
            + delta.seconds * 1_000_000_000
            + delta.microseconds * 1_000
        )
    return None


def _duration_ns(value: Any) -> int | None:
    if isinstance(value, np.timedelta64):
        normalized = value.astype("timedelta64[ns]").astype(np.int64)
        return int(cast(int, normalized))
    if isinstance(value, dt.timedelta):
        # pandas.Timedelta has the same sub-microsecond issue as Timestamp.
        try:
            exact_value = getattr(value, "value", None)
        except (OverflowError, ValueError):
            exact_value = None
        if isinstance(exact_value, (int, np.integer)):
            return int(exact_value)
        duration_value = value
        return (
            duration_value.days * 86_400_000_000_000
            + duration_value.seconds * 1_000_000_000
            + duration_value.microseconds * 1_000
        )
    return None


def _numeric_equal(reference: Any, candidate: Any, policy: ComparisonPolicy) -> bool:
    if is_nan(reference) or is_nan(candidate):
        return is_nan(reference) and is_nan(candidate) and policy.nan_equal
    try:
        left = float(reference)
        right = float(candidate)
    except (TypeError, ValueError, OverflowError):
        return bool(reference == candidate)
    if left == 0.0 and right == 0.0 and not policy.signed_zero_equal:
        return math.copysign(1.0, left) == math.copysign(1.0, right)
    return math.isclose(left, right, rel_tol=policy.rtol, abs_tol=policy.atol)


def _value_mismatches(
    reference: Any,
    candidate: Any,
    policy: ComparisonPolicy,
    path: str,
    *,
    max_mismatches: int,
) -> list[Mismatch]:
    reference = canonicalize(reference)
    candidate = canonicalize(candidate)

    reference_null = is_null(reference)
    candidate_null = is_null(candidate)
    reference_nan = is_nan(reference)
    candidate_nan = is_nan(candidate)
    if policy.null_nan_equal and (
        (reference_null and candidate_nan) or (reference_nan and candidate_null)
    ):
        return []
    if reference_null or candidate_null:
        if reference_null and candidate_null and policy.null_equal:
            return []
        return [
            _mismatch(
                MismatchKind.VALUE,
                "null values are not equivalent"
                if reference_null and candidate_null
                else "null differs from a value",
                path,
                reference,
                candidate,
            )
        ]

    if isinstance(reference, ExceptionInfo) or isinstance(candidate, ExceptionInfo):
        return _exception_mismatches(reference, candidate, policy, path)

    if isinstance(reference, CanonicalFrame) or isinstance(candidate, CanonicalFrame):
        if not isinstance(reference, CanonicalFrame) or not isinstance(candidate, CanonicalFrame):
            return [
                _mismatch(
                    MismatchKind.SCHEMA,
                    "one result is tabular and the other is not",
                    path,
                    reference,
                    candidate,
                )
            ]
        return _frame_mismatches(reference, candidate, policy, path, max_mismatches)

    if isinstance(reference, CanonicalSeries) or isinstance(candidate, CanonicalSeries):
        if not isinstance(reference, CanonicalSeries) or not isinstance(candidate, CanonicalSeries):
            return [
                _mismatch(
                    MismatchKind.SCHEMA,
                    "one result is a series and the other is not",
                    path,
                    reference,
                    candidate,
                )
            ]
        return _series_mismatches(reference, candidate, policy, path, max_mismatches)

    if isinstance(reference, Mapping) or isinstance(candidate, Mapping):
        if not isinstance(reference, Mapping) or not isinstance(candidate, Mapping):
            return [
                _mismatch(
                    MismatchKind.VALUE,
                    "mapping differs from non-mapping",
                    path,
                    reference,
                    candidate,
                )
            ]
        mismatches: list[Mismatch] = []
        reference_keys = set(reference)
        candidate_keys = set(candidate)
        if reference_keys != candidate_keys:
            mismatches.append(
                _mismatch(
                    MismatchKind.SCHEMA,
                    "mapping keys differ",
                    path,
                    sorted(map(str, reference_keys)),
                    sorted(map(str, candidate_keys)),
                    missing=sorted(map(str, reference_keys - candidate_keys)),
                    unexpected=sorted(map(str, candidate_keys - reference_keys)),
                )
            )
        # Comparison stops after a bounded number of mismatches. Set iteration
        # order depends on PYTHONHASHSEED, so sort before the cap is applied or
        # an identical output can acquire a different primary signature.
        for key in sorted(reference_keys & candidate_keys, key=_stable_mapping_key):
            mismatches.extend(
                _value_mismatches(
                    reference[key],
                    candidate[key],
                    policy,
                    f"{path}[{key!r}]",
                    max_mismatches=max_mismatches - len(mismatches),
                )
            )
            if len(mismatches) >= max_mismatches:
                break
        return mismatches[:max_mismatches]

    sequence_types = (tuple, list)
    if isinstance(reference, sequence_types) or isinstance(candidate, sequence_types):
        if not isinstance(reference, sequence_types) or not isinstance(candidate, sequence_types):
            return [
                _mismatch(
                    MismatchKind.VALUE,
                    "sequence differs from non-sequence",
                    path,
                    reference,
                    candidate,
                )
            ]
        mismatches = []
        if len(reference) != len(candidate):
            mismatches.append(
                _mismatch(
                    MismatchKind.SHAPE,
                    "sequence lengths differ",
                    path,
                    len(reference),
                    len(candidate),
                )
            )
        for index, (left, right) in enumerate(zip(reference, candidate, strict=False)):
            mismatches.extend(
                _value_mismatches(
                    left,
                    right,
                    policy,
                    f"{path}[{index}]",
                    max_mismatches=max_mismatches - len(mismatches),
                )
            )
            if len(mismatches) >= max_mismatches:
                break
        return mismatches[:max_mismatches]

    if isinstance(reference, (set, frozenset)) or isinstance(candidate, (set, frozenset)):
        if not isinstance(reference, (set, frozenset)) or not isinstance(
            candidate, (set, frozenset)
        ):
            return [
                _mismatch(
                    MismatchKind.VALUE, "set differs from non-set", path, reference, candidate
                )
            ]
        unmatched = list(candidate)
        for left in reference:
            match_index = next(
                (
                    index
                    for index, right in enumerate(unmatched)
                    if not _value_mismatches(left, right, policy, path, max_mismatches=1)
                ),
                None,
            )
            if match_index is None:
                return [
                    _mismatch(MismatchKind.VALUE, "set members differ", path, reference, candidate)
                ]
            unmatched.pop(match_index)
        if unmatched:
            return [_mismatch(MismatchKind.VALUE, "set members differ", path, reference, candidate)]
        return []

    reference_dt = _datetime_ns(reference)
    candidate_dt = _datetime_ns(candidate)
    if reference_dt is not None or candidate_dt is not None:
        equal = (
            reference_dt is not None
            and candidate_dt is not None
            and abs(reference_dt - candidate_dt) <= policy.datetime_tolerance_ns
        )
        return (
            []
            if equal
            else [
                _mismatch(MismatchKind.VALUE, "datetime values differ", path, reference, candidate)
            ]
        )

    reference_duration = _duration_ns(reference)
    candidate_duration = _duration_ns(candidate)
    if reference_duration is not None or candidate_duration is not None:
        equal = (
            reference_duration is not None
            and candidate_duration is not None
            and abs(reference_duration - candidate_duration) <= policy.datetime_tolerance_ns
        )
        return (
            []
            if equal
            else [
                _mismatch(MismatchKind.VALUE, "duration values differ", path, reference, candidate)
            ]
        )

    numeric = (int, float, decimal.Decimal, np.number)
    if (
        isinstance(reference, numeric)
        and not isinstance(reference, bool)
        and isinstance(candidate, numeric)
        and not isinstance(candidate, bool)
    ):
        return (
            []
            if _numeric_equal(reference, candidate, policy)
            else [
                _mismatch(
                    MismatchKind.VALUE,
                    "numeric values differ beyond tolerance",
                    path,
                    reference,
                    candidate,
                    rtol=policy.rtol,
                    atol=policy.atol,
                )
            ]
        )

    if isinstance(reference, bool) != isinstance(candidate, bool):
        return [
            _mismatch(
                MismatchKind.VALUE,
                "boolean differs from a non-boolean value",
                path,
                reference,
                candidate,
            )
        ]

    try:
        equal = reference == candidate
        if isinstance(equal, np.ndarray):
            equal = bool(equal.all())
    except (TypeError, ValueError):
        equal = False
    return (
        []
        if equal
        else [_mismatch(MismatchKind.VALUE, "values differ", path, reference, candidate)]
    )


def _exception_mismatches(
    reference: Any,
    candidate: Any,
    policy: ComparisonPolicy,
    path: str,
) -> list[Mismatch]:
    if not isinstance(reference, ExceptionInfo) or not isinstance(candidate, ExceptionInfo):
        return [
            _mismatch(
                MismatchKind.EXCEPTION,
                "one implementation raised and the other returned",
                path,
                reference,
                candidate,
            )
        ]
    if not policy.check_exceptions:
        return []
    reference_type = (reference.module, reference.type_name)
    candidate_type = (candidate.module, candidate.type_name)
    if reference_type != candidate_type or reference.message != candidate.message:
        return [
            _mismatch(
                MismatchKind.EXCEPTION,
                "raised exceptions differ",
                path,
                reference,
                candidate,
                reference_type=".".join(part for part in reference_type if part),
                candidate_type=".".join(part for part in candidate_type if part),
            )
        ]
    return []


def _dtype_mismatch(
    reference: Any, candidate: Any, policy: ComparisonPolicy, path: str
) -> Mismatch | None:
    if policy.dtype == "ignore":
        return None
    if policy.dtype == "strict":
        equal = reference.dtype == candidate.dtype
    else:
        families = {reference.family, candidate.family}
        values_are_all_null = all(
            is_null(value) or (policy.null_nan_equal and is_nan(value))
            for value in (*getattr(reference, "values", ()), *getattr(candidate, "values", ()))
        )
        equal = (
            reference.family == candidate.family
            or families <= {"integer", "float", "decimal"}
            # Arrow infers the physical ``null`` dtype for an all-null pandas
            # object column, while typed engines retain the declared dtype.
            # Compatible mode compares the observed domain; strict mode still
            # catches the physical schema difference.
            or ("null" in families and values_are_all_null)
        )
    if equal:
        return None
    return _mismatch(
        MismatchKind.DTYPE,
        "column dtype differs" if hasattr(reference, "name") else "dtype differs",
        path,
        reference.dtype,
        candidate.dtype,
        reference_family=reference.family,
        candidate_family=candidate.family,
        policy=policy.dtype,
    )


def _series_mismatches(
    reference: CanonicalSeries,
    candidate: CanonicalSeries,
    policy: ComparisonPolicy,
    path: str,
    max_mismatches: int,
) -> list[Mismatch]:
    mismatches: list[Mismatch] = []
    left_name = None if reference.name is None else _name_key(reference.name, policy)
    right_name = None if candidate.name is None else _name_key(candidate.name, policy)
    if left_name != right_name:
        mismatches.append(
            _mismatch(
                MismatchKind.COLUMN, "series names differ", path, reference.name, candidate.name
            )
        )
    if mismatch := _dtype_mismatch(reference, candidate, policy, path):
        mismatches.append(mismatch)
    if len(reference.values) != len(candidate.values):
        mismatches.append(
            _mismatch(
                MismatchKind.SHAPE,
                "series lengths differ",
                path,
                len(reference.values),
                len(candidate.values),
            )
        )
    for index, (left, right) in enumerate(zip(reference.values, candidate.values, strict=False)):
        mismatches.extend(
            _value_mismatches(
                left,
                right,
                policy,
                f"{path}[{index}]",
                max_mismatches=max_mismatches - len(mismatches),
            )
        )
        if len(mismatches) >= max_mismatches:
            break
    return mismatches[:max_mismatches]


def _filtered_columns(frame: CanonicalFrame, policy: ComparisonPolicy) -> list[Any]:
    ignored = {_name_key(name, policy) for name in policy.ignored_columns}
    return [column for column in frame.columns if _name_key(column.name, policy) not in ignored]


def _row_equal(left: tuple[Any, ...], right: tuple[Any, ...], policy: ComparisonPolicy) -> bool:
    return len(left) == len(right) and all(
        not _value_mismatches(a, b, policy, "$row", max_mismatches=1)
        for a, b in zip(left, right, strict=True)
    )


def _maximum_row_matching(
    left_rows: tuple[tuple[Any, ...], ...],
    right_rows: tuple[tuple[Any, ...], ...],
    policy: ComparisonPolicy,
) -> tuple[dict[int, int], dict[int, int]]:
    """Return a deterministic maximum bipartite matching of equivalent rows."""

    def bucket_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
        # Exact policy-aware identities make common identical and reordered
        # frames linear. Every proposed pair is still verified by _row_equal;
        # a bucket collision or tolerance-only match therefore cannot change
        # semantics and merely falls through to the complete matcher below.
        return tuple(
            ("scalar", token)
            if (token := _key_token(value, policy)) is not None
            else ("fallback", _stable_mapping_key(value))
            for value in row
        )

    left_match: dict[int, int] = {}
    right_match: dict[int, int] = {}
    buckets: dict[tuple[Any, ...], deque[int]] = {}
    for right_index, right_row in enumerate(right_rows):
        buckets.setdefault(bucket_key(right_row), deque()).append(right_index)
    for left_index, left_row in enumerate(left_rows):
        candidates = buckets.get(bucket_key(left_row))
        if not candidates:
            continue
        right_index = candidates[0]
        if _row_equal(left_row, right_rows[right_index], policy):
            candidates.popleft()
            left_match[left_index] = right_index
            right_match[right_index] = left_index

    if len(left_match) == min(len(left_rows), len(right_rows)):
        return left_match, right_match

    adjacency_cache: dict[int, list[int]] = {}

    def adjacency(left_index: int) -> list[int]:
        edges = adjacency_cache.get(left_index)
        if edges is None:
            left_row = left_rows[left_index]
            edges = [
                right_index
                for right_index, right_row in enumerate(right_rows)
                if _row_equal(left_row, right_row, policy)
            ]
            adjacency_cache[left_index] = edges
        return edges

    # Deterministic augmenting paths avoid greedy false negatives from
    # overlapping tolerance windows. Breadth-first reconstruction is iterative,
    # so large duplicate groups cannot exhaust Python's recursion limit. The
    # deliberately unkeyed fallback can materialize O(left*right) edges in its
    # worst ambiguous case; keyed alignment is the scalable linear option.
    for root in range(len(left_rows)):
        if root in left_match:
            continue
        queue = deque([root])
        # A reached left vertex records the matched right edge that led to it;
        # the root has no parent. Each right records its preceding left edge.
        parent_left: dict[int, int | None] = {root: None}
        parent_right: dict[int, int] = {}
        free_right: int | None = None
        while queue and free_right is None:
            left_index = queue.popleft()
            for right_index in adjacency(left_index):
                if right_index in parent_right:
                    continue
                parent_right[right_index] = left_index
                matched_left = right_match.get(right_index)
                if matched_left is None:
                    free_right = right_index
                    break
                if matched_left not in parent_left:
                    parent_left[matched_left] = right_index
                    queue.append(matched_left)
        if free_right is None:
            continue
        right_index = free_right
        while True:
            left_index = parent_right[right_index]
            previous_right = parent_left[left_index]
            left_match[left_index] = right_index
            right_match[right_index] = left_index
            if previous_right is None:
                break
            right_index = previous_right
    return left_match, right_match


def _numeric_key(value: Any) -> tuple[str, int, int] | None:
    """Return an exact cross-numeric identity without lossy float conversion."""

    if isinstance(value, bool) or is_nan(value):
        return None
    if isinstance(value, (int, np.integer)):
        fraction = fractions.Fraction(int(value), 1)
    elif isinstance(value, (float, np.floating)):
        try:
            numerator, denominator = float(value).as_integer_ratio()
        except (OverflowError, ValueError):
            return None
        fraction = fractions.Fraction(numerator, denominator)
    elif isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return None
        fraction = fractions.Fraction(value)
    else:
        return None
    return "number", fraction.numerator, fraction.denominator


def _key_token(value: Any, policy: ComparisonPolicy) -> tuple[Any, ...] | None:
    """Return an exact, hashable identity for one supported scalar key cell."""

    if is_null(value):
        if not policy.null_equal:
            return None
        return ("missing",) if policy.null_nan_equal else ("null",)
    if is_nan(value):
        if not policy.nan_equal:
            return None
        return ("missing",) if policy.null_nan_equal else ("nan",)
    if isinstance(value, bool):
        return "bool", value
    # NumPy datetime and duration scalars are also np.number instances, so
    # preserve their exact temporal identity before considering numeric keys.
    datetime_value = _datetime_ns(value)
    if datetime_value is not None:
        return "datetime", datetime_value
    duration_value = _duration_ns(value)
    if duration_value is not None:
        return "duration", duration_value
    if isinstance(value, decimal.Decimal) and value.is_infinite():
        return "number-infinity", "negative" if value.is_signed() else "positive"
    if isinstance(value, (float, np.floating)) and math.isinf(float(value)):
        return "number-infinity", "negative" if float(value) < 0 else "positive"
    if numeric := _numeric_key(value):
        if numeric[1] == 0 and not policy.signed_zero_equal:
            try:
                sign = math.copysign(1.0, float(value))
            except (TypeError, ValueError, OverflowError):
                sign = 1.0
            return *numeric, "negative" if sign < 0 else "positive"
        return numeric
    if isinstance(value, dt.date):
        return "date", value.toordinal()
    if isinstance(value, dt.time):
        return "time", value.isoformat()
    if isinstance(value, str):
        return "string", value
    if isinstance(value, bytes):
        return "bytes", value
    if isinstance(
        value,
        (tuple, list, dict, set, frozenset, Mapping, CanonicalFrame, CanonicalSeries),
    ):
        return None
    try:
        hash(value)
    except (TypeError, ValueError):
        return None
    return type(value).__module__, type(value).__qualname__, value


def _keyed_frame_mismatches(
    left_rows: tuple[tuple[Any, ...], ...],
    right_rows: tuple[tuple[Any, ...], ...],
    left_columns: list[Any],
    policy: ComparisonPolicy,
    path: str,
    max_mismatches: int,
) -> list[Mismatch]:
    """Align unique rows by exact composite key, then compare their cells."""

    column_keys = [_name_key(column.name, policy) for column in left_columns]
    configured_keys = [_name_key(name, policy) for name in policy.row_keys]
    if any(key not in column_keys for key in configured_keys):
        return [
            _mismatch(
                MismatchKind.COLUMN,
                "configured row key columns are unavailable",
                path,
                key_columns=policy.row_keys,
            )
        ]
    key_indexes = tuple(column_keys.index(key) for key in configured_keys)

    def side_path(side: str, row_index: int) -> str:
        return f"${side}[{row_index}]" if path == "$" else f"{path}.{side}[{row_index}]"

    def build_index(
        rows: tuple[tuple[Any, ...], ...], side: str
    ) -> tuple[dict[tuple[Any, ...], int], list[Mismatch]]:
        index: dict[tuple[Any, ...], int] = {}
        failures: list[Mismatch] = []
        for row_index, row in enumerate(rows):
            raw_key = tuple(row[column_index] for column_index in key_indexes)
            tokens = tuple(_key_token(value, policy) for value in raw_key)
            if any(token is None for token in tokens):
                nested = any(
                    isinstance(value, (tuple, list, dict, set, frozenset, Mapping))
                    for value in raw_key
                )
                failures.append(
                    _mismatch(
                        MismatchKind.ROW,
                        (
                            "row key contains a non-scalar value"
                            if nested
                            else "row key is not alignable under the comparison policy"
                        ),
                        side_path(side, row_index),
                        key_columns=policy.row_keys,
                        row_index=row_index,
                        side=side,
                    )
                )
                continue
            identity = cast(tuple[Any, ...], tokens)
            previous = index.get(identity)
            if previous is not None:
                failures.append(
                    _mismatch(
                        MismatchKind.ROW,
                        "row keys are not unique",
                        side_path(side, row_index),
                        key_columns=policy.row_keys,
                        first_row=previous,
                        duplicate_row=row_index,
                        side=side,
                    )
                )
            else:
                index[identity] = row_index
        return index, failures

    left_by_key, left_failures = build_index(left_rows, "reference")
    right_by_key, right_failures = build_index(right_rows, "candidate")
    structural = [*left_failures, *right_failures]
    if structural:
        return structural[:max_mismatches]

    mismatches: list[Mismatch] = []
    for identity, left_index in left_by_key.items():
        right_index = right_by_key.get(identity)
        if right_index is None:
            mismatches.append(
                _mismatch(
                    MismatchKind.ROW,
                    "reference row key has no candidate row",
                    f"{path}[{left_index}]",
                    key_columns=policy.row_keys,
                    reference_row=left_index,
                )
            )
            if len(mismatches) >= max_mismatches:
                return mismatches
            continue
        left_row = left_rows[left_index]
        right_row = right_rows[right_index]
        for column_index, (left, right) in enumerate(zip(left_row, right_row, strict=True)):
            cell_mismatches = _value_mismatches(
                left,
                right,
                policy,
                f"{path}[{left_index}].{left_columns[column_index].name}",
                max_mismatches=max_mismatches - len(mismatches),
            )
            for mismatch in cell_mismatches:
                mismatch.details.update(
                    {
                        "alignment": "keyed",
                        "key_columns": list(policy.row_keys),
                        "reference_row": left_index,
                        "candidate_row": right_index,
                    }
                )
            mismatches.extend(cell_mismatches)
            if len(mismatches) >= max_mismatches:
                return mismatches
    for identity, right_index in right_by_key.items():
        if identity not in left_by_key:
            mismatches.append(
                _mismatch(
                    MismatchKind.ROW,
                    "candidate row key has no reference row",
                    side_path("candidate", right_index),
                    key_columns=policy.row_keys,
                    candidate_row=right_index,
                )
            )
            if len(mismatches) >= max_mismatches:
                break
    return mismatches[:max_mismatches]


def _frame_mismatches(
    reference: CanonicalFrame,
    candidate: CanonicalFrame,
    policy: ComparisonPolicy,
    path: str,
    max_mismatches: int,
) -> list[Mismatch]:
    mismatches: list[Mismatch] = []
    left_columns = _filtered_columns(reference, policy)
    right_columns = _filtered_columns(candidate, policy)
    left_keys = [_name_key(column.name, policy) for column in left_columns]
    right_keys = [_name_key(column.name, policy) for column in right_columns]

    if len(set(left_keys)) != len(left_keys) or len(set(right_keys)) != len(right_keys):
        return [
            _mismatch(
                MismatchKind.SCHEMA,
                "column names are ambiguous under the selected name policy",
                path,
                [column.name for column in left_columns],
                [column.name for column in right_columns],
            )
        ]

    if policy.row_order == "keyed":
        configured_keys = {_name_key(name, policy) for name in policy.row_keys}
        if not configured_keys <= set(left_keys) or not configured_keys <= set(right_keys):
            return [
                _mismatch(
                    MismatchKind.COLUMN,
                    "configured row key columns are unavailable",
                    path,
                    key_columns=policy.row_keys,
                )
            ]

    if set(left_keys) != set(right_keys):
        mismatches.append(
            _mismatch(
                MismatchKind.COLUMN,
                "column sets differ",
                path,
                [column.name for column in left_columns],
                [column.name for column in right_columns],
                missing=[
                    left_columns[left_keys.index(key)].name
                    for key in left_keys
                    if key not in right_keys
                ],
                unexpected=[
                    right_columns[right_keys.index(key)].name
                    for key in right_keys
                    if key not in left_keys
                ],
            )
        )
        return mismatches

    if policy.column_order == "strict" and left_keys != right_keys:
        mismatches.append(
            _mismatch(
                MismatchKind.COLUMN,
                "column order differs",
                path,
                [column.name for column in left_columns],
                [column.name for column in right_columns],
            )
        )

    # From this point candidate columns are aligned to the reference by name;
    # this reports useful cell paths even when a prior order mismatch exists.
    right_by_key = {_name_key(column.name, policy): column for column in right_columns}
    right_columns = [right_by_key[key] for key in left_keys]
    # Keyed alignment can state exactly which identities are missing; a generic
    # shape mismatch would hide that evidence under small mismatch caps.
    if reference.height != candidate.height and policy.row_order != "keyed":
        mismatches.append(
            _mismatch(
                MismatchKind.SHAPE,
                "row counts differ",
                path,
                reference.height,
                candidate.height,
            )
        )

    for left, right in zip(left_columns, right_columns, strict=True):
        if mismatch := _dtype_mismatch(left, right, policy, f"{path}.{left.name}"):
            mismatches.append(mismatch)
            if len(mismatches) >= max_mismatches:
                return mismatches[:max_mismatches]

    left_rows = tuple(
        tuple(column.values[index] for column in left_columns) for index in range(reference.height)
    )
    right_rows = tuple(
        tuple(column.values[index] for column in right_columns) for index in range(candidate.height)
    )
    if policy.row_order == "ignore":
        # Complete the semantic decision before applying the output cap. A
        # contract-level mismatch above must not change whether rows can pair.
        left_match, right_match = _maximum_row_matching(left_rows, right_rows, policy)
        for left_index, left_row in enumerate(left_rows):
            if left_index not in left_match:
                mismatches.append(
                    _mismatch(
                        MismatchKind.ROW,
                        "reference row has no equivalent candidate row",
                        f"{path}[{left_index}]",
                        left_row,
                        None,
                    )
                )
                if len(mismatches) >= max_mismatches:
                    break
        for right_index, right_row in enumerate(right_rows):
            if right_index in right_match:
                continue
            if len(mismatches) >= max_mismatches:
                break
            mismatches.append(
                _mismatch(
                    MismatchKind.ROW,
                    "candidate contains an unmatched row",
                    f"{path}[{right_index}]",
                    None,
                    right_row,
                )
            )
        return mismatches[:max_mismatches]

    if policy.row_order == "keyed":
        keyed_mismatches = _keyed_frame_mismatches(
            left_rows,
            right_rows,
            left_columns,
            policy,
            path,
            max_mismatches,
        )
        mismatches.extend(keyed_mismatches[: max_mismatches - len(mismatches)])
        return mismatches[:max_mismatches]

    for row_index, (left_row, right_row) in enumerate(zip(left_rows, right_rows, strict=False)):
        for column_index, (left, right) in enumerate(zip(left_row, right_row, strict=True)):
            mismatches.extend(
                _value_mismatches(
                    left,
                    right,
                    policy,
                    f"{path}[{row_index}].{left_columns[column_index].name}",
                    max_mismatches=max_mismatches - len(mismatches),
                )
            )
            if len(mismatches) >= max_mismatches:
                return mismatches[:max_mismatches]
    return mismatches[:max_mismatches]


def _observation_value(observation: Any) -> Any:
    """Extract a result from execution.Observation without importing it."""

    outcome = str(getattr(observation, "outcome", ""))
    if outcome.endswith("returned"):
        table = getattr(observation, "table", None)
        if table is not None:
            return table
        if getattr(observation, "has_value", False):
            return getattr(observation, "value", None)
        return None
    exception = getattr(observation, "exception", None)
    if exception is not None:
        return ExceptionInfo(
            type_name=str(getattr(exception, "type", getattr(exception, "type_name", "Exception"))),
            message=str(getattr(exception, "message", "")),
            module=str(getattr(exception, "module", "builtins")),
        )
    label = outcome.rsplit(".", 1)[-1] or "execution_error"
    return ExceptionInfo(type_name=label, message=label, module="parity.execution")


def compare_observations(
    reference: Any,
    candidate: Any,
    policy: ComparisonPolicy | None = None,
    *,
    max_mismatches: int = 100,
) -> list[Mismatch]:
    """Compare two duck-typed execution observations, including mutation."""

    selected = policy or ComparisonPolicy()
    mismatches: list[Mismatch] = []
    if selected.check_input_mutation:
        reference_mutated = bool(getattr(reference, "mutated_input", False))
        candidate_mutated = bool(getattr(candidate, "mutated_input", False))
        reference_labels = getattr(reference, "mutated_inputs", None)
        candidate_labels = getattr(candidate, "mutated_inputs", None)
        if isinstance(reference_labels, tuple) and isinstance(candidate_labels, tuple):
            reference_set = {str(label) for label in reference_labels}
            candidate_set = {str(label) for label in candidate_labels}
            inconsistent_sides = [
                label
                for label, aggregate, labels in (
                    ("reference", reference_mutated, reference_set),
                    ("candidate", candidate_mutated, candidate_set),
                )
                if aggregate != bool(labels)
            ]
            if inconsistent_sides:
                mismatches.append(
                    _mismatch(
                        MismatchKind.MUTATION,
                        "input mutation metadata is inconsistent",
                        "$inputs",
                        reference_mutated,
                        candidate_mutated,
                        sides=inconsistent_sides,
                    )
                )
            for label in sorted(reference_set ^ candidate_set):
                if len(mismatches) >= max_mismatches:
                    break
                escaped = label.replace("~", "~0").replace("/", "~1")
                mismatches.append(
                    _mismatch(
                        MismatchKind.MUTATION,
                        "input mutation behaviour differs",
                        f"$inputs/{escaped}",
                        label in reference_set,
                        label in candidate_set,
                        input=label,
                    )
                )
        elif len(mismatches) < max_mismatches and reference_mutated != candidate_mutated:
            # Retain the legacy aggregate check for old/duck-typed observations.
            mismatches.append(
                _mismatch(
                    MismatchKind.MUTATION,
                    "input mutation behaviour differs",
                    "$input",
                    reference_mutated,
                    candidate_mutated,
                )
            )
    if len(mismatches) < max_mismatches:
        mismatches.extend(
            compare(
                _observation_value(reference),
                _observation_value(candidate),
                selected,
                max_mismatches=max_mismatches - len(mismatches),
            )
        )
    return mismatches[:max_mismatches]


def compare(
    reference: Any,
    candidate: Any,
    policy: ComparisonPolicy | None = None,
    *,
    max_mismatches: int = 100,
) -> list[Mismatch]:
    """Return every semantic mismatch up to a caller-controlled safety cap."""

    if max_mismatches < 1:
        raise ValueError("max_mismatches must be at least 1")
    selected = policy or ComparisonPolicy()
    return _value_mismatches(
        reference,
        candidate,
        selected,
        "$",
        max_mismatches=max_mismatches,
    )[:max_mismatches]


def compare_result(
    reference: Any,
    candidate: Any,
    policy: ComparisonPolicy | None = None,
    *,
    max_mismatches: int = 100,
) -> ComparisonResult:
    """Return an immutable comparison result with ``equivalent`` convenience."""

    return ComparisonResult(
        tuple(compare(reference, candidate, policy, max_mismatches=max_mismatches))
    )


def equivalent(
    reference: Any,
    candidate: Any,
    policy: ComparisonPolicy | None = None,
) -> bool:
    """Return whether two values are semantically equivalent."""

    return not compare(reference, candidate, policy)


__all__ = [
    "ComparisonResult",
    "compare",
    "compare_observations",
    "compare_result",
    "equivalent",
    "mismatch_signature",
]
