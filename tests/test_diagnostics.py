from parity.diagnostics import diagnose
from parity.models import Mismatch, MismatchKind


def test_diagnoses_missing_value_dtype_difference() -> None:
    diagnoses = diagnose(
        [
            Mismatch(
                kind=MismatchKind.DTYPE,
                path="amount",
                message="null column is Float64 versus Int64",
            )
        ]
    )
    assert [item.code for item in diagnoses] == ["missing-values", "dtype-resolution"]


def test_diagnoses_unmatched_row_content_without_claiming_order() -> None:
    diagnoses = diagnose([Mismatch(kind=MismatchKind.ROW, message="row 0 differs", path="[0]")])
    assert [item.code for item in diagnoses] == ["row-content"]
    assert "keyed or order-insensitive" in diagnoses[0].evidence[0]


def test_diagnoses_explicit_row_order_evidence() -> None:
    diagnoses = diagnose(
        [Mismatch(kind=MismatchKind.VALUE, message="row order differs", path="$result")]
    )
    assert [item.code for item in diagnoses] == ["row-order"]


def test_falls_back_to_generic_diagnosis() -> None:
    diagnoses = diagnose([Mismatch(kind=MismatchKind.VALUE, message="unexpected value")])
    assert diagnoses[0].code == "semantic-difference"


def test_no_mismatches_have_no_diagnosis() -> None:
    assert diagnose([]) == []


def test_candidate_word_does_not_trigger_datetime_diagnosis() -> None:
    diagnoses = diagnose(
        [
            Mismatch(
                kind=MismatchKind.EXCEPTION,
                message="candidate could not be executed",
                path="$candidate",
            )
        ]
    )
    assert {item.code for item in diagnoses} == {"exception-contract"}
