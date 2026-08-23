"""Canonical call inputs and configuration-driven invocation generation."""

from __future__ import annotations

import json
import keyword
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias

import pyarrow as pa
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from parity.adapters import load_arrow_fixture, to_arrow
from parity.generation import (
    adversarial_bundle_cases,
    adversarial_cases,
    bundle_strategy,
    frame_strategy,
)
from parity.models import (
    Cardinality,
    EqualRowCount,
    FrameArgument,
    FrameSchema,
    FrameSequenceArgument,
    InputBundle,
    InputSpec,
    InvocationArgument,
    InvocationConfig,
    JsonArgument,
    JsonValue,
    KeyOverlap,
)
from parity.schema import infer_schema, rows_satisfy_frame_constraints, validate_bundle_schemas

_MAX_CALL_ARGUMENTS = 256
_MAX_FRAME_SEQUENCE_ITEMS = 256
_MAX_KEYWORD_LENGTH = 128
_MAX_JSON_VALUE_BYTES = 256 * 1024
_MAX_INVOCATION_JSON_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class FrameSequence:
    """A dataframe collection passed to one callable argument."""

    items: tuple[pa.Table, ...]
    container: str = "list"

    def __post_init__(self) -> None:
        if self.container not in {"list", "tuple"}:
            raise ValueError("frame sequence container must be 'list' or 'tuple'")
        if len(self.items) > _MAX_FRAME_SEQUENCE_ITEMS:
            raise ValueError("frame sequence contains more than 256 items")
        try:
            normalized = tuple(to_arrow(item).combine_chunks() for item in self.items)
        except Exception as error:
            raise TypeError("frame sequence items must be supported dataframes") from error
        object.__setattr__(self, "items", normalized)


InvocationValue: TypeAlias = pa.Table | FrameSequence | JsonValue


@dataclass(frozen=True, slots=True)
class Invocation:
    """One complete, canonical ``callable(*args, **kwargs)`` input."""

    args: tuple[InvocationValue, ...] = ()
    kwargs: Mapping[str, InvocationValue] = MappingProxyType({})

    def __post_init__(self) -> None:
        if len(self.args) > _MAX_CALL_ARGUMENTS or len(self.kwargs) > _MAX_CALL_ARGUMENTS:
            raise ValueError("invocation contains more than 256 positional or keyword arguments")
        normalized_args = tuple(_normalize_value(value) for value in self.args)
        normalized_kwargs: dict[str, InvocationValue] = {}
        for name, value in self.kwargs.items():
            if (
                not isinstance(name, str)
                or not name.isidentifier()
                or keyword.iskeyword(name)
                or len(name) > _MAX_KEYWORD_LENGTH
            ):
                raise ValueError(
                    "invocation keyword names must be Python identifiers of at most 128 characters"
                )
            normalized_kwargs[name] = _normalize_value(value)
        json_bytes = sum(
            _json_size(value)
            for value in (*normalized_args, *normalized_kwargs.values())
            if not isinstance(value, pa.Table | FrameSequence)
        )
        if json_bytes > _MAX_INVOCATION_JSON_BYTES:
            raise ValueError("invocation JSON arguments exceed 512 KiB in total")
        object.__setattr__(self, "args", normalized_args)
        object.__setattr__(self, "kwargs", MappingProxyType(normalized_kwargs))

    def copy(self) -> Invocation:
        """Return a container copy while retaining immutable Arrow tables."""

        return Invocation(self.args, dict(self.kwargs))


@dataclass(frozen=True, slots=True)
class FrameLocation:
    """One Arrow leaf and its stable invocation path."""

    path: str
    table: pa.Table


@dataclass(frozen=True, slots=True)
class ResolvedInvocation:
    """Deterministic examples and one jointly shrinking search strategy."""

    deterministic: tuple[tuple[str, Invocation], ...]
    strategy: SearchStrategy[Invocation] | None


def _json_copy(value: Any) -> JsonValue:
    """Validate a bounded JSON-like value and detach mutable containers."""

    try:
        encoded = json.dumps(value, allow_nan=True, separators=(",", ":"))
        decoded: JsonValue = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise TypeError(
            "invocation values must be dataframes, frame sequences, or JSON-like"
        ) from error
    if len(encoded.encode("utf-8")) > _MAX_JSON_VALUE_BYTES:
        raise ValueError("one invocation JSON value exceeds 256 KiB")
    return decoded


def _json_size(value: JsonValue) -> int:
    return len(json.dumps(value, allow_nan=True, separators=(",", ":")).encode("utf-8"))


def _normalize_value(value: Any) -> InvocationValue:
    if isinstance(value, FrameSequence):
        return FrameSequence(
            tuple(to_arrow(item).combine_chunks() for item in value.items), value.container
        )
    if isinstance(value, pa.Table):
        return value.combine_chunks()
    try:
        return _json_copy(value)
    except TypeError as json_error:
        try:
            return to_arrow(value).combine_chunks()
        except Exception:
            raise json_error from None


def normalize_invocation(value: Any) -> Invocation:
    """Normalize one public custom-generator value."""

    if not isinstance(value, Invocation):
        raise TypeError("custom generators must yield parity.Invocation values")
    return Invocation(value.args, value.kwargs)


def iter_frames(invocation: Invocation) -> Iterator[FrameLocation]:
    """Yield every Arrow leaf in deterministic call order."""

    for index, value in enumerate(invocation.args):
        root = f"args/{index}"
        if isinstance(value, pa.Table):
            yield FrameLocation(root, value)
        elif isinstance(value, FrameSequence):
            for item_index, table in enumerate(value.items):
                yield FrameLocation(f"{root}/{item_index}", table)
    for name, value in invocation.kwargs.items():
        root = "kwargs/" + name.replace("~", "~0").replace("/", "~1")
        if isinstance(value, pa.Table):
            yield FrameLocation(root, value)
        elif isinstance(value, FrameSequence):
            for item_index, table in enumerate(value.items):
                yield FrameLocation(f"{root}/{item_index}", table)


def frame_count(invocation: Invocation) -> int:
    return sum(1 for _ in iter_frames(invocation))


def row_count(invocation: Invocation) -> int:
    return sum(item.table.num_rows for item in iter_frames(invocation))


def _frame_name(argument: FrameArgument, *, index: int | None, keyword: str | None) -> str:
    if argument.name is not None:
        return argument.name
    if keyword is not None:
        return keyword
    assert index is not None
    return f"arg{index}"


@dataclass(frozen=True, slots=True)
class _ResolvedArgument:
    spec: InvocationArgument
    deterministic: tuple[InvocationValue, ...]
    strategy: SearchStrategy[InvocationValue]
    frame_name: str | None = None
    frame_schema: FrameSchema | None = None
    frame_fixture: pa.Table | None = None


def _resolve_frame(
    spec: FrameArgument,
    *,
    name: str,
    adversarial: bool,
) -> _ResolvedArgument:
    fixture = load_arrow_fixture(spec.fixture) if spec.fixture is not None else None
    schema = spec.input_schema or (infer_schema(fixture) if fixture is not None else None)
    if schema is None:
        raise ValueError(f"frame argument {name!r} has no fixture or schema")
    if fixture is not None and not rows_satisfy_frame_constraints(schema, fixture.to_pylist()):
        raise ValueError(f"frame argument {name!r} fixture violates its schema constraints")
    deterministic: list[InvocationValue] = []
    if fixture is not None:
        deterministic.append(fixture)
    if spec.generate and adversarial:
        deterministic.extend(case.table for case in adversarial_cases(schema))
    if not deterministic:
        # A schema-only case still needs at least one deterministic observation
        # when generated search is disabled.
        generated = adversarial_cases(schema)
        if generated:
            deterministic.append(generated[0].table)
    strategy: SearchStrategy[InvocationValue]
    if spec.generate:
        strategy = frame_strategy(schema)
    else:
        assert fixture is not None
        strategy = st.just(fixture)
    return _ResolvedArgument(
        spec,
        tuple(deterministic),
        strategy,
        frame_name=name,
        frame_schema=schema,
        frame_fixture=fixture,
    )


def _resolve_json(spec: JsonArgument) -> _ResolvedArgument:
    values = tuple(_json_copy(value) for value in spec.values)
    return _ResolvedArgument(spec, values, st.sampled_from(values))


def _sequence_schema(
    spec: FrameSequenceArgument, *, name: str
) -> tuple[FrameSchema | None, tuple[pa.Table, ...]]:
    fixtures = tuple(load_arrow_fixture(path) for path in spec.fixtures)
    schema = spec.input_schema or (infer_schema(fixtures[0]) if fixtures else None)
    if schema is None:
        if not spec.generate and not fixtures:
            return None, fixtures
        raise ValueError(f"frame sequence {name!r} has no fixture or schema")
    for fixture in fixtures:
        if not rows_satisfy_frame_constraints(schema, fixture.to_pylist()):
            raise ValueError(f"frame sequence {name!r} fixture violates its schema constraints")
    return schema, fixtures


def _resolve_sequence(
    spec: FrameSequenceArgument,
    *,
    name: str,
    adversarial: bool,
) -> _ResolvedArgument:
    schema, fixtures = _sequence_schema(spec, name=name)
    if schema is None:
        empty = FrameSequence((), spec.container)
        return _ResolvedArgument(spec, (empty,), st.just(empty))
    deterministic: list[InvocationValue] = []
    if spec.min_items <= len(fixtures) <= spec.max_items:
        deterministic.append(FrameSequence(fixtures, spec.container))
    if spec.generate and adversarial:
        table_cases = adversarial_cases(schema)
        lengths = sorted(
            {spec.min_items, min(max(spec.min_items, 1), spec.max_items), spec.max_items}
        )
        for length in lengths:
            if not table_cases and length:
                continue
            items = (
                tuple(table_cases[item % len(table_cases)].table for item in range(length))
                if length
                else ()
            )
            deterministic.append(FrameSequence(items, spec.container))
    if not deterministic:
        if spec.min_items == 0:
            deterministic.append(FrameSequence((), spec.container))
        else:
            table_cases = adversarial_cases(schema)
            if not table_cases:
                raise ValueError(
                    f"frame sequence {name!r} has no deterministic valid frame example"
                )
            deterministic.append(
                FrameSequence(
                    tuple(table_cases[0].table for _ in range(spec.min_items)),
                    spec.container,
                )
            )
    strategy: SearchStrategy[InvocationValue]
    if spec.generate:
        strategy = st.lists(
            frame_strategy(schema), min_size=spec.min_items, max_size=spec.max_items
        ).map(lambda items: FrameSequence(tuple(items), spec.container))
    else:
        strategy = st.just(FrameSequence(fixtures, spec.container))
    return _ResolvedArgument(spec, tuple(deterministic), strategy)


def _resolve_argument(
    spec: InvocationArgument,
    *,
    name: str,
    adversarial: bool,
) -> _ResolvedArgument:
    if isinstance(spec, FrameArgument):
        return _resolve_frame(spec, name=name, adversarial=adversarial)
    if isinstance(spec, JsonArgument):
        return _resolve_json(spec)
    return _resolve_sequence(spec, name=name, adversarial=adversarial)


def _build_invocation(
    args: Sequence[InvocationValue],
    kwargs: Mapping[str, InvocationValue],
    varargs: FrameSequence | None,
) -> Invocation:
    positional = list(args)
    if varargs is not None:
        positional.extend(varargs.items)
    return Invocation(tuple(positional), kwargs)


def resolve_invocation(
    config: InvocationConfig,
    *,
    adversarial: bool,
    search: bool,
) -> ResolvedInvocation:
    """Load fixtures and build deterministic and shrinking invocation examples."""

    def positional_name(spec: InvocationArgument, index: int) -> str:
        if isinstance(spec, FrameArgument):
            return _frame_name(spec, index=index, keyword=None)
        if isinstance(spec, FrameSequenceArgument):
            return spec.name or f"arg{index}"
        return f"arg{index}"

    def keyword_name(spec: InvocationArgument, name: str) -> str:
        if isinstance(spec, FrameArgument):
            return _frame_name(spec, index=None, keyword=name)
        if isinstance(spec, FrameSequenceArgument):
            return spec.name or name
        return name

    positional = [
        _resolve_argument(
            spec,
            name=positional_name(spec, index),
            adversarial=adversarial,
        )
        for index, spec in enumerate(config.args)
    ]
    keywords = {
        name: _resolve_argument(
            spec,
            name=keyword_name(spec, name),
            adversarial=adversarial,
        )
        for name, spec in config.kwargs.items()
    }
    varargs = (
        _resolve_sequence(
            config.varargs,
            name=config.varargs.name or "varargs",
            adversarial=adversarial,
        )
        if config.varargs is not None
        else None
    )

    named_frames = [
        resolved
        for resolved in [*positional, *keywords.values()]
        if resolved.frame_name is not None
    ]
    relationship_names: set[str] = set()
    for relationship in config.relationships:
        if isinstance(relationship, EqualRowCount):
            relationship_names.update(relationship.inputs)
        elif isinstance(relationship, KeyOverlap | Cardinality):
            relationship_names.update((relationship.left.input, relationship.right.input))
        else:
            relationship_names.update((relationship.child.input, relationship.parent.input))
    related_frames = [
        resolved for resolved in named_frames if resolved.frame_name in relationship_names
    ]
    frame_bundle_strategy: SearchStrategy[dict[str, pa.Table]] | None = None
    frame_bundle_deterministic: list[dict[str, pa.Table]] = []
    if config.relationships:
        inputs = {
            resolved.frame_name: InputSpec(
                fixture=None,
                input_schema=resolved.frame_schema,
            )
            for resolved in related_frames
            if resolved.frame_name is not None
        }
        bundle = InputBundle(inputs=inputs, relationships=config.relationships)
        schemas = {
            resolved.frame_name: resolved.frame_schema
            for resolved in related_frames
            if resolved.frame_name is not None and resolved.frame_schema is not None
        }
        validate_bundle_schemas(bundle, schemas)
        frame_bundle_strategy = bundle_strategy(bundle, schemas)
        related_fixtures = {
            resolved.frame_name: resolved.frame_fixture
            for resolved in related_frames
            if resolved.frame_name is not None and resolved.frame_fixture is not None
        }
        fixtures = (
            {name: table for name, table in related_fixtures.items() if table is not None}
            if len(related_fixtures) == len(related_frames)
            else None
        )
        relationship_cases = [
            case.tables for case in adversarial_bundle_cases(bundle, schemas, fixtures=fixtures)
        ]
        frame_bundle_deterministic = relationship_cases if adversarial else relationship_cases[:1]
        if not frame_bundle_deterministic:
            raise ValueError(
                "invocation relationships have no deterministic valid example; loosen the "
                "frame constraints or supply a custom generator"
            )

    def baseline(resolved: _ResolvedArgument) -> InvocationValue:
        return resolved.deterministic[0]

    base_args = [baseline(resolved) for resolved in positional]
    base_kwargs = {name: baseline(resolved) for name, resolved in keywords.items()}
    base_varargs = baseline(varargs) if varargs is not None else None
    assert base_varargs is None or isinstance(base_varargs, FrameSequence)
    if frame_bundle_deterministic:
        tables = frame_bundle_deterministic[0]
        for position, resolved in enumerate(positional):
            if resolved.frame_name in tables:
                base_args[position] = tables[resolved.frame_name]
        for name, resolved in keywords.items():
            if resolved.frame_name in tables:
                base_kwargs[name] = tables[resolved.frame_name]
    deterministic: list[tuple[str, Invocation]] = [
        ("invocation:baseline", _build_invocation(base_args, base_kwargs, base_varargs))
    ]

    # Cover every declared discrete/boundary value without a Cartesian explosion:
    # vary one logical argument at a time around the stable baseline.
    for index, resolved in enumerate(positional):
        if resolved.frame_name in relationship_names:
            continue
        for value_index, value in enumerate(resolved.deterministic[1:], start=1):
            args = list(base_args)
            args[index] = value
            deterministic.append(
                (
                    f"invocation:args/{index}:{value_index}",
                    _build_invocation(args, base_kwargs, base_varargs),
                )
            )
    for name, resolved in keywords.items():
        if resolved.frame_name in relationship_names:
            continue
        for value_index, value in enumerate(resolved.deterministic[1:], start=1):
            kwargs = dict(base_kwargs)
            kwargs[name] = value
            deterministic.append(
                (
                    f"invocation:kwargs/{name}:{value_index}",
                    _build_invocation(base_args, kwargs, base_varargs),
                )
            )
    if varargs is not None:
        for value_index, value in enumerate(varargs.deterministic[1:], start=1):
            assert isinstance(value, FrameSequence)
            deterministic.append(
                (
                    f"invocation:varargs:{value_index}",
                    _build_invocation(base_args, base_kwargs, value),
                )
            )

    if frame_bundle_deterministic:
        for index, tables in enumerate(frame_bundle_deterministic[1:], start=1):
            args = list(base_args)
            kwargs = dict(base_kwargs)
            for position, resolved in enumerate(positional):
                if resolved.frame_name in tables:
                    args[position] = tables[resolved.frame_name]
            for name, resolved in keywords.items():
                if resolved.frame_name in tables:
                    kwargs[name] = tables[resolved.frame_name]
            deterministic.append(
                (f"invocation:relationships:{index}", _build_invocation(args, kwargs, base_varargs))
            )

    # Stable de-duplication uses Arrow content hashes only indirectly through IPC
    # in the artifact layer. Duplicate deterministic calls are cheap and retain
    # their useful source labels, so no data serialization is performed here.
    if not search:
        return ResolvedInvocation(tuple(deterministic), None)

    positional_strategy: SearchStrategy[tuple[InvocationValue, ...]] = st.tuples(
        *(resolved.strategy for resolved in positional)
    )
    keyword_strategy: SearchStrategy[dict[str, InvocationValue]] = st.fixed_dictionaries(
        {name: resolved.strategy for name, resolved in keywords.items()}
    )
    varargs_strategy: SearchStrategy[FrameSequence | None] = (
        varargs.strategy.map(lambda value: value if isinstance(value, FrameSequence) else None)
        if varargs is not None
        else st.none()
    )

    if frame_bundle_strategy is None:
        strategy = st.builds(
            _build_invocation, positional_strategy, keyword_strategy, varargs_strategy
        )
    else:

        def bind_bundle(
            args: tuple[InvocationValue, ...],
            kwargs: dict[str, InvocationValue],
            extra: FrameSequence | None,
            tables: dict[str, pa.Table],
        ) -> Invocation:
            mutable_args = list(args)
            mutable_kwargs = dict(kwargs)
            for position, resolved in enumerate(positional):
                if resolved.frame_name in tables:
                    mutable_args[position] = tables[resolved.frame_name]
            for name, resolved in keywords.items():
                if resolved.frame_name in tables:
                    mutable_kwargs[name] = tables[resolved.frame_name]
            return _build_invocation(mutable_args, mutable_kwargs, extra)

        strategy = st.builds(
            bind_bundle,
            positional_strategy,
            keyword_strategy,
            varargs_strategy,
            frame_bundle_strategy,
        )
    return ResolvedInvocation(tuple(deterministic), strategy)


__all__ = [
    "FrameLocation",
    "FrameSequence",
    "Invocation",
    "InvocationValue",
    "ResolvedInvocation",
    "frame_count",
    "iter_frames",
    "normalize_invocation",
    "resolve_invocation",
    "row_count",
]
