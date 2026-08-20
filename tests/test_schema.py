from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pytest

from parity.models import (
    Cardinality,
    ColumnSchema,
    EqualRowCount,
    ForeignKey,
    FrameSchema,
    InputBundle,
    InputSpec,
    KeyOverlap,
    KeyRef,
    RowComparison,
    SortedBy,
)
from parity.schema import (
    arrow_schema,
    infer_schema,
    rows_satisfy_frame_constraints,
    sort_rows_for_constraints,
    table_from_rows,
    validate_bundle_schemas,
    validate_frame_schema,
)


def test_infer_portable_schema_and_observed_constraints() -> None:
    frame = pd.DataFrame(
        {
            "id": pd.Series([1, 2, 3], dtype="int64"),
            "amount": [1.5, None, 4.0],
            "label": pd.Series(["a", "b", "a"], dtype="category"),
            "when": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
        }
    )
    schema = infer_schema(frame, max_rows=12)
    assert [column.dtype for column in schema.columns] == [
        "integer",
        "float",
        "category",
        "datetime",
    ]
    assert schema.columns[0].unique
    assert schema.columns[0].minimum == 1
    assert schema.columns[0].maximum == 3
    assert schema.columns[2].categories == ["a", "b"]
    assert schema.columns[3].timezone == "UTC"
    assert schema.max_rows == 12


def test_infer_rejects_columnless_frame() -> None:
    with pytest.raises(ValueError, match="no columns"):
        infer_schema(pa.table({}))


def test_arrow_schema_preserves_name_nullable_and_timezone() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="id", dtype="int32", nullable=False),
            ColumnSchema(name="at", dtype="datetime", timezone="UTC"),
        ]
    )
    result = arrow_schema(schema)
    assert result.names == ["id", "at"]
    assert result.field("id").type == pa.int32()
    assert not result.field("id").nullable
    assert result.field("at").type == pa.timestamp("us", tz="UTC")


def test_table_from_rows_retains_empty_dtypes() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="id", dtype="integer", nullable=False)])
    table = table_from_rows(schema, [])
    assert table.num_rows == 0
    assert table.schema.field("id").type == pa.int64()


def test_table_from_rows_supports_temporal_values() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="day", dtype="date")])
    table = table_from_rows(schema, [{"day": dt.date(2024, 2, 29)}])
    assert table.column("day").to_pylist() == [dt.date(2024, 2, 29)]


def test_table_from_rows_preserves_both_ambiguous_timezone_instants() -> None:
    zone = ZoneInfo("America/New_York")
    first = dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0)
    second = dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=1)
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="at",
                dtype="datetime",
                nullable=False,
                timezone="America/New_York",
            )
        ]
    )

    table = table_from_rows(schema, [{"at": first}, {"at": second}])
    stored_instants = table.column("at").cast(pa.int64()).to_pylist()
    round_tripped_instants = [value.astimezone(dt.UTC) for value in table.column("at").to_pylist()]

    assert stored_instants == [1730611800000000, 1730615400000000]
    assert round_tripped_instants == [
        dt.datetime(2024, 11, 3, 5, 30, tzinfo=dt.UTC),
        dt.datetime(2024, 11, 3, 6, 30, tzinfo=dt.UTC),
    ]


def test_table_from_rows_rejects_values_outside_declared_dtype() -> None:
    schema = FrameSchema(columns=[ColumnSchema(name="id", dtype="int64")])
    with pytest.raises(ValueError, match="declared dtype"):
        table_from_rows(schema, [{"id": "not-an-integer"}])


@pytest.mark.parametrize(
    "column",
    [
        ColumnSchema(
            name="code",
            dtype="string",
            regex=r"[A-Z]{2}[0-9]{2}",
            categories=["AB12", "bad"],
        ),
        ColumnSchema(
            name="code",
            dtype="string",
            min_length=4,
            categories=["valid", "no"],
        ),
    ],
)
def test_schema_rejects_categories_outside_text_constraints(column: ColumnSchema) -> None:
    with pytest.raises(ValueError, match="categorical values outside"):
        validate_frame_schema(FrameSchema(columns=[column]))


def test_schema_rejects_examples_outside_the_complete_column_domain() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="code",
                dtype="string",
                nullable=False,
                regex=r"[A-Z]{2}[0-9]{2}",
                min_length=4,
                max_length=4,
                categories=["AB12", "CD34"],
                examples=["outside"],
            )
        ]
    )

    with pytest.raises(ValueError, match="examples outside its declared domain"):
        validate_frame_schema(schema)


def test_schema_accepts_iso_datetime_examples_in_the_configured_zone() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="at",
                dtype="datetime",
                timezone="America/New_York",
                minimum="2024-01-01T00:00:00",
                maximum="2024-12-31T23:59:59",
                examples=["2024-11-03T01:30:00-04:00"],
            )
        ]
    )

    validate_frame_schema(schema)


def test_sorted_by_uses_composite_lexicographic_order_and_null_placement() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="group", dtype="string", nullable=False),
            ColumnSchema(name="value", dtype="integer"),
        ],
        constraints=[SortedBy(columns=["group", "value"], nulls="first")],
    )
    rows = [
        {"group": "b", "value": 0},
        {"group": "a", "value": 2},
        {"group": "a", "value": None},
        {"group": "a", "value": 1},
    ]

    sorted_rows = sort_rows_for_constraints(schema, rows)

    assert sorted_rows == [
        {"group": "a", "value": None},
        {"group": "a", "value": 1},
        {"group": "a", "value": 2},
        {"group": "b", "value": 0},
    ]
    assert rows_satisfy_frame_constraints(schema, sorted_rows)
    assert not rows_satisfy_frame_constraints(schema, rows)


def test_row_comparison_ignores_rows_with_null_values() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="start", dtype="integer"),
            ColumnSchema(name="end", dtype="integer"),
        ],
        constraints=[RowComparison(left="start", operator="le", right="end")],
    )

    assert rows_satisfy_frame_constraints(
        schema,
        [{"start": None, "end": -10}, {"start": 1, "end": 1}],
    )
    assert not rows_satisfy_frame_constraints(schema, [{"start": 2, "end": 1}])


def test_validate_frame_schema_rejects_impossible_comparison_domains() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="start", dtype="integer", nullable=False, minimum=10, maximum=20),
            ColumnSchema(name="end", dtype="integer", nullable=False, minimum=0, maximum=5),
        ],
        constraints=[RowComparison(left="start", operator="le", right="end")],
    )

    with pytest.raises(ValueError, match="has no satisfying values"):
        validate_frame_schema(schema)


def test_validate_frame_schema_accepts_vacuous_nullable_comparison() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="start", dtype="integer", minimum=10, maximum=20),
            ColumnSchema(name="end", dtype="integer", nullable=False, minimum=0, maximum=5),
        ],
        min_rows=1,
        constraints=[RowComparison(left="start", operator="le", right="end")],
    )

    validate_frame_schema(schema)


def test_validate_frame_schema_rejects_row_comparison_unique_capacity() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="left",
                dtype="integer",
                nullable=False,
                unique=True,
                categories=[1, 2],
            ),
            ColumnSchema(
                name="right",
                dtype="integer",
                nullable=False,
                unique=True,
                categories=[2, 3],
            ),
        ],
        min_rows=2,
        max_rows=2,
        constraints=[RowComparison(left="left", operator="eq", right="right")],
    )

    with pytest.raises(ValueError, match="only 1 distinct values"):
        validate_frame_schema(schema)


def test_nullable_peer_retains_full_unique_capacity_under_row_comparison() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(
                name="left",
                dtype="integer",
                nullable=False,
                unique=True,
                categories=[1, 2, 3],
            ),
            ColumnSchema(
                name="right",
                dtype="integer",
                nullable=True,
                categories=[1, None],
            ),
        ],
        min_rows=3,
        max_rows=3,
        constraints=[RowComparison(left="left", operator="eq", right="right")],
    )

    validate_frame_schema(schema)


def test_validate_frame_schema_rejects_overlapping_row_comparisons() -> None:
    schema = FrameSchema(
        columns=[
            ColumnSchema(name="a", dtype="integer"),
            ColumnSchema(name="b", dtype="integer"),
        ],
        constraints=[
            RowComparison(left="a", operator="le", right="b"),
            RowComparison(left="b", operator="le", right="b"),
        ],
    )

    with pytest.raises(ValueError, match="overlapping row_comparison"):
        validate_frame_schema(schema)


def _two_input_bundle(
    left: FrameSchema,
    right: FrameSchema,
    relationship: KeyOverlap | Cardinality | EqualRowCount,
) -> tuple[InputBundle, dict[str, FrameSchema]]:
    schemas = {"left": left, "right": right}
    return (
        InputBundle(
            inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
            relationships=[relationship],
        ),
        schemas,
    )


def test_cardinality_does_not_require_key_domain_overlap() -> None:
    left = FrameSchema(columns=[ColumnSchema(name="key", dtype="string", categories=["left"])])
    right = FrameSchema(columns=[ColumnSchema(name="key", dtype="string", categories=["right"])])
    bundle, schemas = _two_input_bundle(
        left,
        right,
        Cardinality(
            left=KeyRef(input="left", columns=["key"]),
            right=KeyRef(input="right", columns=["key"]),
            relationship="many_to_many",
        ),
    )

    validate_bundle_schemas(bundle, schemas)


def test_key_overlap_rejects_disjoint_concrete_numeric_domains() -> None:
    left = FrameSchema(columns=[ColumnSchema(name="key", dtype="int8", minimum=0, maximum=1)])
    right = FrameSchema(columns=[ColumnSchema(name="key", dtype="int64", minimum=2, maximum=3)])
    bundle, schemas = _two_input_bundle(
        left,
        right,
        KeyOverlap(
            left=KeyRef(input="left", columns=["key"]),
            right=KeyRef(input="right", columns=["key"]),
        ),
    )

    with pytest.raises(ValueError, match="no common non-null value"):
        validate_bundle_schemas(bundle, schemas)


def test_equal_row_count_accounts_for_finite_schema_uniqueness() -> None:
    left = FrameSchema(
        columns=[ColumnSchema(name="key", dtype="boolean", nullable=False, unique=True)],
        max_rows=4,
    )
    right = FrameSchema(
        columns=[ColumnSchema(name="key", dtype="boolean", nullable=False)],
        min_rows=3,
        max_rows=4,
    )
    bundle, schemas = _two_input_bundle(
        left,
        right,
        EqualRowCount(inputs=["left", "right"]),
    )

    with pytest.raises(ValueError, match="incompatible row ranges"):
        validate_bundle_schemas(bundle, schemas)


def test_bundle_rejects_overlapping_non_identical_keys_on_one_input() -> None:
    schemas = {
        "left": FrameSchema(
            columns=[
                ColumnSchema(name="a", dtype="integer"),
                ColumnSchema(name="b", dtype="integer"),
            ]
        ),
        "right": FrameSchema(
            columns=[
                ColumnSchema(name="a", dtype="integer"),
                ColumnSchema(name="b", dtype="integer"),
            ]
        ),
    }
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            KeyOverlap(
                left=KeyRef(input="left", columns=["a"]),
                right=KeyRef(input="right", columns=["a"]),
            ),
            KeyOverlap(
                left=KeyRef(input="left", columns=["a", "b"]),
                right=KeyRef(input="right", columns=["a", "b"]),
            ),
        ],
    )

    with pytest.raises(ValueError, match="overlapping non-identical key references"):
        validate_bundle_schemas(bundle, schemas)


def test_bundle_rejects_category_outside_concrete_integer_dtype() -> None:
    left = FrameSchema(
        columns=[
            ColumnSchema(
                name="key",
                dtype="int8",
                nullable=False,
                categories=[1_000],
            )
        ],
        min_rows=1,
    )
    right = FrameSchema(
        columns=[
            ColumnSchema(
                name="key",
                dtype="int64",
                nullable=False,
                categories=[1_000],
            )
        ],
        min_rows=1,
    )
    bundle, schemas = _two_input_bundle(
        left,
        right,
        KeyOverlap(
            left=KeyRef(input="left", columns=["key"]),
            right=KeyRef(input="right", columns=["key"]),
        ),
    )

    with pytest.raises(ValueError, match="no values representable"):
        validate_bundle_schemas(bundle, schemas)


def test_bundle_rejects_foreign_key_cycle() -> None:
    schemas = {
        name: FrameSchema(columns=[ColumnSchema(name="key", dtype="integer")])
        for name in ("left", "right")
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            ForeignKey(child=refs["left"], parent=refs["right"]),
            ForeignKey(child=refs["right"], parent=refs["left"]),
        ],
    )

    with pytest.raises(ValueError, match="cannot contain a cycle"):
        validate_bundle_schemas(bundle, schemas)


def test_equal_row_count_rejects_a_schema_with_no_representable_rows() -> None:
    left = FrameSchema(
        columns=[
            ColumnSchema(
                name="key",
                dtype="int8",
                nullable=False,
                categories=[1_000],
            )
        ],
        min_rows=0,
        max_rows=2,
    )
    right = FrameSchema(
        columns=[ColumnSchema(name="key", dtype="int8", nullable=False)],
        min_rows=1,
        max_rows=2,
    )
    bundle, schemas = _two_input_bundle(
        left,
        right,
        EqualRowCount(inputs=["left", "right"]),
    )

    with pytest.raises(ValueError, match="no values representable"):
        validate_bundle_schemas(bundle, schemas)


def test_overlap_rejects_empty_transitive_foreign_key_parent() -> None:
    schemas = {
        "child": FrameSchema(
            columns=[
                ColumnSchema(
                    name="key",
                    dtype="string",
                    nullable=True,
                    categories=["shared", None],
                )
            ],
            min_rows=1,
            max_rows=1,
        ),
        "parent": FrameSchema(
            columns=[ColumnSchema(name="key", dtype="string", categories=["shared"])],
            min_rows=0,
            max_rows=0,
        ),
        "peer": FrameSchema(
            columns=[ColumnSchema(name="key", dtype="string", categories=["shared"])],
            min_rows=1,
            max_rows=1,
        ),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            ForeignKey(child=refs["child"], parent=refs["parent"], allow_nulls=True),
            KeyOverlap(left=refs["child"], right=refs["peer"]),
        ],
    )

    with pytest.raises(ValueError, match="transitive foreign-key input"):
        validate_bundle_schemas(bundle, schemas)


def test_disjoint_overlap_edges_require_separate_rows_at_shared_input() -> None:
    schemas = {
        "left": FrameSchema(
            columns=[ColumnSchema(name="key", dtype="string", categories=["left"])],
            min_rows=1,
            max_rows=1,
        ),
        "middle": FrameSchema(
            columns=[ColumnSchema(name="key", dtype="string", categories=["left", "right"])],
            min_rows=1,
            max_rows=1,
        ),
        "right": FrameSchema(
            columns=[ColumnSchema(name="key", dtype="string", categories=["right"])],
            min_rows=1,
            max_rows=1,
        ),
    }
    refs = {name: KeyRef(input=name, columns=["key"]) for name in schemas}
    bundle = InputBundle(
        inputs={name: InputSpec(input_schema=schema) for name, schema in schemas.items()},
        relationships=[
            KeyOverlap(left=refs["left"], right=refs["middle"]),
            KeyOverlap(left=refs["middle"], right=refs["right"]),
        ],
    )

    with pytest.raises(ValueError, match="more distinct keys than the row capacity"):
        validate_bundle_schemas(bundle, schemas)
