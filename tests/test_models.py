from __future__ import annotations

import pytest
from pydantic import ValidationError

from parity.models import (
    CallableSpec,
    CaseConfig,
    ColumnSchema,
    FrameSchema,
    GenerationConfig,
    InputBundle,
    InputSpec,
    ParityConfig,
    RowComparison,
    SortedBy,
    Status,
    SuiteResult,
)


def schema() -> FrameSchema:
    return FrameSchema(
        columns=[ColumnSchema(name="amount", dtype="float64", nullable=False)],
        min_rows=1,
        max_rows=5,
    )


def test_frame_schema_rejects_duplicate_columns() -> None:
    with pytest.raises(ValidationError, match="column names must be unique"):
        FrameSchema(
            columns=[
                ColumnSchema(name="id", dtype="int64"),
                ColumnSchema(name="id", dtype="int64"),
            ]
        )


def test_frame_constraints_parse_defaults_and_discriminator() -> None:
    schema = FrameSchema.model_validate(
        {
            "columns": [
                {"name": "start", "dtype": "integer"},
                {"name": "end", "dtype": "integer"},
            ],
            "constraints": [
                {"kind": "sorted_by", "columns": ["start"]},
                {
                    "kind": "row_comparison",
                    "left": "start",
                    "operator": "le",
                    "right": "end",
                },
            ],
        }
    )

    assert schema.constraints == [
        SortedBy(columns=["start"], descending=False, nulls="last"),
        RowComparison(left="start", operator="le", right="end"),
    ]


def test_frame_constraints_validate_references_duplicates_and_comparability() -> None:
    columns = [
        ColumnSchema(name="number", dtype="integer"),
        ColumnSchema(name="label", dtype="string"),
    ]
    with pytest.raises(ValidationError, match="unknown columns"):
        FrameSchema(columns=columns, constraints=[SortedBy(columns=["missing"])])
    with pytest.raises(ValidationError, match="must be unique"):
        FrameSchema(
            columns=columns,
            constraints=[
                RowComparison(left="number", operator="le", right="number"),
                RowComparison(left="number", operator="ge", right="number"),
            ],
        )
    with pytest.raises(ValidationError, match="comparable scalar dtype families"):
        FrameSchema(
            columns=columns,
            constraints=[RowComparison(left="number", operator="eq", right="label")],
        )
    with pytest.raises(ValidationError, match="at most one sorted_by"):
        FrameSchema(
            columns=columns,
            constraints=[SortedBy(columns=["number"]), SortedBy(columns=["label"])],
        )


def test_generation_stability_repeats_is_bounded() -> None:
    assert GenerationConfig().stability_repeats == 2
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        GenerationConfig(stability_repeats=0)
    with pytest.raises(ValidationError, match="less than or equal to 10"):
        GenerationConfig(stability_repeats=11)


def test_frame_constraint_models_are_exported_from_public_package() -> None:
    from parity import FrameConstraint as PublicFrameConstraint
    from parity import RowComparison as PublicRowComparison
    from parity import SortedBy as PublicSortedBy

    assert PublicFrameConstraint is not None
    assert PublicSortedBy is SortedBy
    assert PublicRowComparison is RowComparison


def test_frame_schema_rejects_unknown_composite_key_column() -> None:
    with pytest.raises(ValidationError, match="unknown columns"):
        FrameSchema(
            columns=[ColumnSchema(name="id", dtype="int64")],
            unique_together=[["missing"]],
        )


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        ([[]], "cannot be empty"),
        ([["id", "id"]], "within a unique_together group"),
        ([["id"], ["id"]], "groups must be unique"),
    ],
)
def test_frame_schema_rejects_degenerate_unique_together_groups(
    groups: list[list[str]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        FrameSchema(
            columns=[ColumnSchema(name="id", dtype="int64")],
            unique_together=groups,
        )


def test_column_categories_are_unique_and_respect_nullability() -> None:
    with pytest.raises(ValidationError, match="unique values"):
        ColumnSchema(name="group", dtype="string", categories=["a", "a"])
    with pytest.raises(ValidationError, match="non-nullable"):
        ColumnSchema(
            name="group",
            dtype="string",
            nullable=False,
            categories=["a", None],
        )


def test_input_bundle_rejects_partial_fixture_sets() -> None:
    with pytest.raises(ValidationError, match="provided for every input or none"):
        InputBundle(
            inputs={
                "left": InputSpec(fixture="left.csv"),
                "right": InputSpec(input_schema=schema()),
            }
        )


def test_input_bundle_requires_callable_safe_names() -> None:
    with pytest.raises(ValidationError, match="non-keyword Python identifier"):
        InputBundle(
            inputs={
                "valid": InputSpec(input_schema=schema()),
                "class": InputSpec(input_schema=schema()),
            }
        )


def test_keyword_bundle_rejects_ambiguous_static_arguments() -> None:
    bundle = InputBundle(
        inputs={
            "left": InputSpec(input_schema=schema()),
            "right": InputSpec(input_schema=schema()),
        }
    )
    base = {
        "name": "join",
        "reference": CallableSpec(target="example:reference"),
        "candidate": CallableSpec(target="example:candidate"),
        "input_bundle": bundle,
    }
    with pytest.raises(ValidationError, match="cannot be combined with static_args"):
        CaseConfig(**base, static_args=[1])
    with pytest.raises(ValidationError, match="collide with static_kwargs"):
        CaseConfig(**base, static_kwargs={"left": 1})

    positional = bundle.model_copy(update={"binding": "positional"})
    assert CaseConfig(**{**base, "input_bundle": positional}, static_args=[1]).static_args == [1]


def test_case_requires_fixture_or_schema() -> None:
    with pytest.raises(ValidationError, match="fixture or schema"):
        CaseConfig(
            name="orders",
            reference=CallableSpec(target="example:reference"),
            candidate=CallableSpec(target="example:candidate"),
        )


def test_callable_pandas_input_defaults_to_arrow_and_rejects_unknown_modes() -> None:
    assert CallableSpec(target="example:reference").pandas_input == "arrow"
    assert CallableSpec(target="example:reference", pandas_input="native").pandas_input == "native"
    with pytest.raises(ValidationError, match="pandas_input"):
        CallableSpec.model_validate(
            {"target": "example:reference", "pandas_input": "numpy_nullable"}
        )


def test_callable_record_distributions_are_explicit_normalized_and_unique() -> None:
    spec = CallableSpec(
        target="example:reference",
        record_distributions=["skrub", "Scikit_Learn"],
    )
    assert spec.record_distributions == ["scikit-learn", "skrub"]
    with pytest.raises(ValidationError, match="duplicate distribution"):
        CallableSpec(
            target="example:reference",
            record_distributions=["Scikit-Learn", "scikit_learn"],
        )
    with pytest.raises(ValidationError, match="ASCII"):
        CallableSpec(target="example:reference", record_distributions=["private/package"])


def test_case_accepts_schema_alias_and_serializes_it() -> None:
    case = CaseConfig.model_validate(
        {
            "name": "orders",
            "reference": {"target": "example:reference"},
            "candidate": {"target": "example:candidate"},
            "schema": schema().model_dump(),
        }
    )
    assert case.input_schema is not None
    assert "schema" in case.model_dump(by_alias=True)


def test_config_rejects_duplicate_case_names() -> None:
    case = CaseConfig(
        name="orders",
        reference=CallableSpec(target="example:reference"),
        candidate=CallableSpec(target="example:candidate"),
        schema=schema(),
    )
    with pytest.raises(ValidationError, match="case names must be unique"):
        ParityConfig(cases=[case, case.model_copy(deep=True)])


def test_suite_passed_property() -> None:
    assert SuiteResult(status=Status.PASSED, cases=[]).passed
    assert not SuiteResult(status=Status.FAILED, cases=[]).passed
