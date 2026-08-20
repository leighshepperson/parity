from __future__ import annotations

import json
from collections.abc import Callable

import pyarrow as pa
from pydantic import BaseModel, ValidationError

import parity.engine as engine
from parity.canonical import ExceptionInfo as CanonicalExceptionInfo
from parity.canonical import Raise, Return
from parity.comparison import compare, compare_observations, mismatch_signature
from parity.exception_semantics import normalize_exception_message
from parity.execution import ExceptionInfo, ExecutionOutcome, Observation
from parity.models import ComparisonPolicy, MismatchKind, RunMetrics, Status


def _returned(value: object = None) -> Observation:
    return Observation(
        outcome=ExecutionOutcome.RETURNED,
        value=value,  # type: ignore[arg-type]
        has_value=True,
        metrics=RunMetrics(duration_seconds=0),
    )


def _raised(type_name: str, message: str, *, module: str = "builtins") -> Observation:
    return Observation(
        outcome=ExecutionOutcome.RAISED,
        exception=ExceptionInfo(module, type_name, message),
        metrics=RunMetrics(duration_seconds=0),
    )


def _signature(candidate: Observation) -> str:
    mismatches = compare_observations(_returned(None), candidate)
    assert [mismatch.kind for mismatch in mismatches] == [MismatchKind.EXCEPTION]
    return mismatch_signature(mismatches)


def test_numpy_exception_findings_do_not_collapse() -> None:
    candidates = {
        "overflow": _raised("OverflowError", "Python integer 256 out of bounds for uint8"),
        "copy": _raised(
            "ValueError",
            "Unable to avoid copy while creating an array as requested. "
            "If using np.array(obj, copy=False), replace it.",
        ),
        "float": _raised(
            "AttributeError",
            "`np.float_` was removed in the NumPy 2.0 release. Use `np.float64` instead.",
        ),
        "complex": _raised(
            "AttributeError",
            "`np.complex_` was removed in the NumPy 2.0 release. Use `np.complex128` instead.",
        ),
        "cast": _raised("AttributeError", "`np.cast` was removed in the NumPy 2.0 release."),
        "ptp": _raised(
            "AttributeError",
            "`ndarray.ptp` was removed in NumPy 2.0. Use np.ptp(arr, ...) instead.",
        ),
        "product": _raised("AttributeError", "module 'numpy' has no attribute 'product'"),
    }

    signatures = {name: _signature(observation) for name, observation in candidates.items()}

    assert all(signature.startswith("ms3:") for signature in signatures.values())
    assert len(set(signatures.values())) == len(signatures)


def test_exception_signatures_ignore_volatile_run_details() -> None:
    first = _raised(
        "RuntimeError",
        "failed for 'customer-a' at /tmp/run-123/item.py:42 on 2026-08-20T10:15:30Z "
        "with object <Widget object at 0x1234abcd>, request "
        "550e8400-e29b-41d4-a716-446655440000, dependency 2.0.1",
    )
    second = _raised(
        "RuntimeError",
        "failed for 'customer-b' at /private/var/run-999/item.py:987 on "
        "2027-09-21T11:16:31Z with object <Widget object at 0x9876fedc>, request "
        "123e4567-e89b-42d3-a456-426614174000, dependency 3.4.5",
    )

    assert first.exception is not None
    assert second.exception is not None
    assert first.exception.fingerprint == second.exception.fingerprint
    assert _signature(first) == _signature(second)


def test_api_subject_remains_semantic_when_versions_and_witnesses_change() -> None:
    old_witness = _raised(
        "AttributeError",
        "`np.float_` was removed in the NumPy 2.0 release for value 'customer-a'",
    )
    new_witness = _raised(
        "AttributeError",
        "`np.float_` was removed in the NumPy 2.4.1 release for value 'customer-b'",
    )
    different_api = _raised(
        "AttributeError",
        "`np.complex_` was removed in the NumPy 2.4.1 release for value 'customer-b'",
    )

    assert _signature(old_witness) == _signature(new_witness)
    assert _signature(old_witness) != _signature(different_api)


class _IntegerModel(BaseModel):
    amount: int


class _OtherIntegerModel(BaseModel):
    quantity: int


class _RequiredModel(BaseModel):
    name: str


def _validation_error(call: Callable[[], object]) -> ValidationError:
    try:
        call()
    except ValidationError as error:
        return error
    raise AssertionError("expected ValidationError")


def test_pydantic_error_codes_are_structured_stable_semantics() -> None:
    integer_a = ExceptionInfo.from_exception(
        _validation_error(lambda: _IntegerModel(amount="private-a"))
    )
    integer_b = ExceptionInfo.from_exception(
        _validation_error(lambda: _OtherIntegerModel(quantity="private-b"))
    )
    missing = ExceptionInfo.from_exception(_validation_error(lambda: _RequiredModel()))

    assert integer_a.details == {
        "error_codes": ["int_parsing"],
        "location_shapes": ["field"],
    }
    assert integer_a.fingerprint == integer_b.fingerprint
    assert integer_a.fingerprint != missing.fingerprint
    assert (
        compare_observations(
            Observation(
                outcome=ExecutionOutcome.RAISED,
                exception=integer_a,
                metrics=RunMetrics(duration_seconds=0),
            ),
            Observation(
                outcome=ExecutionOutcome.RAISED,
                exception=integer_b,
                metrics=RunMetrics(duration_seconds=0),
            ),
        )
        == []
    )

    mismatches = compare_observations(
        Observation(
            outcome=ExecutionOutcome.RAISED,
            exception=integer_a,
            metrics=RunMetrics(duration_seconds=0),
        ),
        Observation(
            outcome=ExecutionOutcome.RAISED,
            exception=missing,
            metrics=RunMetrics(duration_seconds=0),
        ),
    )
    assert mismatch_signature(mismatches).startswith("ms3:")


def test_return_and_raise_are_distinct_even_when_return_value_is_exception_shaped() -> None:
    exception = CanonicalExceptionInfo("ValueError", "bad value 123", "builtins")

    mismatches = compare(Return(exception), Raise(exception))

    assert [mismatch.kind for mismatch in mismatches] == [MismatchKind.EXCEPTION]
    assert mismatches[0].details["reference_outcome"] == "return"
    assert mismatches[0].details["candidate_outcome"] == "raise"


def test_raise_type_and_normalized_message_are_semantic() -> None:
    value = _raised("ValueError", "invalid value 'first'")
    same = _raised("ValueError", "invalid value 'second'")
    other_type = _raised("TypeError", "invalid value 'second'")
    other_reason = _raised("ValueError", "value is required")

    assert compare_observations(value, same) == []
    assert _signature(value) != _signature(other_type)
    assert _signature(value) != _signature(other_reason)


def test_exception_mismatch_artifact_projection_omits_message_values() -> None:
    secret = "customer-secret@example.com"
    candidate = _raised(
        "ValueError",
        f"invalid account '{secret}' at /private/customer/account.json API_KEY=hunter2",
    )

    mismatch = compare_observations(_returned(None), candidate)[0]
    payload = json.dumps(mismatch.model_dump(mode="json"), sort_keys=True)

    assert secret not in payload
    assert "hunter2" not in payload
    assert "/private/customer" not in payload
    assert "exception_fingerprint" in payload


def test_user_raised_timeout_and_execution_named_errors_are_failed_not_error() -> None:
    table = pa.table({"x": [1]})

    def reference(_value: object) -> Observation:
        return _returned(None)

    for module, type_name in (
        ("builtins", "TimeoutError"),
        ("parity.execution", "ExecutionError"),
    ):

        def candidate(
            _value: object,
            selected_module: str = module,
            selected_type: str = type_name,
        ) -> Observation:
            return _raised(selected_type, "domain rejection", module=selected_module)

        _, _, mismatches, status = engine._observe_pair(
            table,
            reference,
            candidate,
            ComparisonPolicy(),
        )
        assert status is Status.FAILED
        assert mismatch_signature(mismatches).startswith("ms3:")


def test_explicit_execution_error_is_infrastructure_error() -> None:
    table = pa.table({"x": [1]})
    infrastructure = Observation(
        outcome=ExecutionOutcome.ERROR,
        exception=ExceptionInfo("parity.execution", "ExecutionError", "boundary failed"),
        metrics=RunMetrics(duration_seconds=0),
    )

    _, _, mismatches, status = engine._observe_pair(
        table,
        lambda _value: _returned(None),
        lambda _value: infrastructure,
        ComparisonPolicy(),
    )

    assert status is Status.ERROR
    assert mismatches[0].message == "candidate could not be executed (ExecutionError)"


def test_normalized_messages_are_bounded_and_remove_direct_identifiers() -> None:
    private = "x" * 10_000
    semantics = normalize_exception_message(
        f"bad value '{private}' at /private/customer/file.txt on 2026-08-20T10:15:30Z"
    )

    assert private not in semantics.pattern
    assert "<value>" in semantics.pattern
    assert "<path>" in semantics.pattern
    assert "<timestamp>" in semantics.pattern
    assert len(semantics.pattern) < 1_000


def test_removed_api_identifiers_are_preserved_as_safe_structured_details() -> None:
    first = ExceptionInfo.from_exception(AttributeError("np.cast was removed for secret value"))
    second = ExceptionInfo.from_exception(AttributeError("np.float_ was removed at /private/x"))

    assert first.details == {"api_tokens": ["np.cast"]}
    assert second.details == {"api_tokens": ["np.float_"]}
    assert first.fingerprint != second.fingerprint
