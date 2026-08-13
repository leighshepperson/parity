"""Cross-library semantic comparison with explicit equivalence policy."""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
import re
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
    if reference.height != candidate.height:
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
        unmatched = list(enumerate(right_rows))
        for left_index, left_row in enumerate(left_rows):
            match_position = next(
                (
                    position
                    for position, (_, right_row) in enumerate(unmatched)
                    if _row_equal(left_row, right_row, policy)
                ),
                None,
            )
            if match_position is None:
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
            else:
                unmatched.pop(match_position)
        for right_index, right_row in unmatched:
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
