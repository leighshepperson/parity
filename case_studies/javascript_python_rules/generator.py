"""Recursive JSON rule programs and three retained migration regressions."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from typing import Any

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from parity import Invocation

Expression = dict[str, Any]


def _const(value: object) -> Expression:
    return {"op": "const", "value": value}


def _var(name: str) -> Expression:
    return {"op": "var", "name": name}


def _binary(operation: str, left: Expression, right: Expression) -> Expression:
    return {"op": operation, "left": left, "right": right}


def _negate(value: Expression) -> Expression:
    return {"op": "not", "value": value}


def _conditional(
    condition: Expression,
    then: Expression,
    otherwise: Expression,
) -> Expression:
    return {"op": "if", "condition": condition, "then": then, "else": otherwise}


@cache
def _integer_expressions(depth: int) -> SearchStrategy[Expression]:
    leaf = st.one_of(
        st.integers(min_value=-10, max_value=10).map(_const),
        st.sampled_from(("x", "y", "z")).map(_var),
    )
    if depth == 0:
        return leaf
    integers = _integer_expressions(depth - 1)
    booleans = _boolean_expressions(depth - 1)
    return st.one_of(
        leaf,
        st.builds(lambda left, right: _binary("add", left, right), integers, integers),
        st.builds(_conditional, booleans, integers, integers),
    )


@cache
def _boolean_expressions(depth: int) -> SearchStrategy[Expression]:
    integers = _integer_expressions(max(depth - 1, 0))
    leaf = st.one_of(
        st.booleans().map(_const),
        st.sampled_from(("enabled", "vip")).map(_var),
        st.builds(lambda left, right: _binary("eq", left, right), integers, integers),
        st.builds(lambda left, right: _binary("lt", left, right), integers, integers),
    )
    if depth == 0:
        return leaf
    booleans = _boolean_expressions(depth - 1)
    return st.one_of(
        leaf,
        st.builds(lambda left, right: _binary("and", left, right), booleans, booleans),
        st.builds(lambda left, right: _binary("or", left, right), booleans, booleans),
        st.builds(_negate, booleans),
    )


@st.composite
def _contexts(draw: st.DrawFn) -> dict[str, object]:
    return {
        "x": draw(st.integers(min_value=-10, max_value=10)),
        "y": draw(st.integers(min_value=-10, max_value=10)),
        "z": draw(st.integers(min_value=-10, max_value=10)),
        "enabled": draw(st.booleans()),
        "vip": draw(st.booleans()),
    }


def _rule(name: str, when: Expression, score: Expression, labels: list[str]) -> dict[str, object]:
    return {"name": name, "when": when, "score": score, "labels": labels}


def _call(program: dict[str, object], context: dict[str, object], threshold: int) -> Invocation:
    return Invocation(args=(program, context), kwargs={"threshold": threshold})


@st.composite
def _recursive_controls(draw: st.DrawFn) -> Invocation:
    context = draw(_contexts())
    when = draw(_boolean_expressions(2))
    score = draw(_integer_expressions(2))
    labels = draw(st.lists(st.sampled_from(("audit", "manual", "priority")), max_size=3))
    program = {"rules": [_rule("generated", when, score, labels)]}
    return _call(program, context, 1_000)


@st.composite
def _semantic_error_controls(draw: st.DrawFn) -> Invocation:
    context = draw(_contexts())
    program = {
        "rules": [
            _rule("invalid", _const(True), _var("missing"), ["invalid"]),
        ]
    }
    return _call(program, context, 1)


@st.composite
def _threshold_regressions(draw: st.DrawFn) -> Invocation:
    value = draw(st.integers(min_value=-10, max_value=10))
    context = draw(_contexts())
    program = {"rules": [_rule("boundary", _const(True), _const(value), ["boundary"])]}
    return _call(program, context, value)


@st.composite
def _first_match_regressions(draw: st.DrawFn) -> Invocation:
    first = draw(st.integers(min_value=-10, max_value=10))
    second = draw(st.sampled_from((-3, -2, -1, 1, 2, 3)))
    context = draw(_contexts())
    program = {
        "rules": [
            _rule("first", _const(True), _const(first), ["first"]),
            _rule("second", _const(True), _const(second), ["second"]),
        ]
    }
    return _call(program, context, 1_000)


@st.composite
def _eager_regressions(draw: st.DrawFn) -> Invocation:
    context = draw(_contexts())
    program = {
        "rules": [
            _rule("guarded", _const(False), _var("missing"), ["unreachable"]),
        ]
    }
    return _call(program, context, 1)


def rule_programs() -> SearchStrategy[Invocation]:
    """Generate recursive controls plus each deliberately injected defect family."""

    return st.one_of(
        _recursive_controls(),
        _semantic_error_controls(),
        _threshold_regressions(),
        _first_match_regressions(),
        _eager_regressions(),
    )


def _regression_eager() -> Invocation:
    return _call(
        {"rules": [_rule("guarded", _const(False), _var("missing"), ["unreachable"])]},
        {"x": 1, "y": 2, "z": 3, "enabled": False, "vip": False},
        1,
    )


def _regression_first_match() -> Invocation:
    return _call(
        {
            "rules": [
                _rule("first", _const(True), _const(1), ["first"]),
                _rule("second", _const(True), _const(2), ["second"]),
            ]
        },
        {"x": 0, "y": 0, "z": 0, "enabled": True, "vip": False},
        1_000,
    )


def _regression_threshold() -> Invocation:
    return _call(
        {"rules": [_rule("boundary", _const(True), _const(2), ["boundary"])]},
        {"x": 0, "y": 0, "z": 0, "enabled": False, "vip": True},
        2,
    )


REGRESSIONS = (
    _regression_eager(),
    _regression_first_match(),
    _regression_threshold(),
)


def regression_eager() -> Iterable[Invocation]:
    return (REGRESSIONS[0],)


def regression_first_match() -> Iterable[Invocation]:
    return (REGRESSIONS[1],)


def regression_threshold() -> Iterable[Invocation]:
    return (REGRESSIONS[2],)
