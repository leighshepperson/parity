from __future__ import annotations

import pytest
from pydantic import ValidationError

from parity.models import (
    CallableSpec,
    CaseConfig,
    ColumnSchema,
    FrameSchema,
    ParityConfig,
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


def test_frame_schema_rejects_unknown_composite_key_column() -> None:
    with pytest.raises(ValidationError, match="unknown columns"):
        FrameSchema(
            columns=[ColumnSchema(name="id", dtype="int64")],
            unique_together=[["missing"]],
        )


def test_case_requires_fixture_or_schema() -> None:
    with pytest.raises(ValidationError, match="fixture or schema"):
        CaseConfig(
            name="orders",
            reference=CallableSpec(target="example:reference"),
            candidate=CallableSpec(target="example:candidate"),
        )


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
