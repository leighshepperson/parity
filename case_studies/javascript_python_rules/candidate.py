"""Correct and deliberately flawed Python ports of the legacy rules engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RuleEvaluationError(Exception):
    """The rules contract rejected a malformed expression or missing variable."""


RuleEvaluationError.__module__ = "legacy.rules"


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuleEvaluationError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise RuleEvaluationError(f"{label} keys must be strings")
    return dict(value)


def _keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RuleEvaluationError(f"invalid fields for {label}")


def _integer(value: object) -> int:
    if type(value) is not int:
        raise RuleEvaluationError("expression must produce an integer")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise RuleEvaluationError("expression must produce a boolean")
    return value


def _same_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _expression(node: object, context: Mapping[str, object]) -> object:
    expression = _object(node, "expression")
    operation = expression.get("op")

    if operation == "const":
        _keys(expression, {"op", "value"}, "const expression")
        value = expression["value"]
        if value is not None and type(value) not in {bool, int, str}:
            raise RuleEvaluationError("const value must be a JSON primitive")
        return value
    if operation == "var":
        _keys(expression, {"op", "name"}, "var expression")
        name = expression["name"]
        if not isinstance(name, str):
            raise RuleEvaluationError("variable name must be a string")
        if name not in context:
            raise RuleEvaluationError(f"unknown variable '{name}'")
        return context[name]
    if operation == "add":
        _keys(expression, {"op", "left", "right"}, "add expression")
        return _integer(_expression(expression["left"], context)) + _integer(
            _expression(expression["right"], context)
        )
    if operation == "eq":
        _keys(expression, {"op", "left", "right"}, "eq expression")
        return _same_value(
            _expression(expression["left"], context),
            _expression(expression["right"], context),
        )
    if operation == "lt":
        _keys(expression, {"op", "left", "right"}, "lt expression")
        return _integer(_expression(expression["left"], context)) < _integer(
            _expression(expression["right"], context)
        )
    if operation == "and":
        _keys(expression, {"op", "left", "right"}, "and expression")
        left = _boolean(_expression(expression["left"], context))
        return left and _boolean(_expression(expression["right"], context))
    if operation == "or":
        _keys(expression, {"op", "left", "right"}, "or expression")
        left = _boolean(_expression(expression["left"], context))
        return left or _boolean(_expression(expression["right"], context))
    if operation == "not":
        _keys(expression, {"op", "value"}, "not expression")
        return not _boolean(_expression(expression["value"], context))
    if operation == "if":
        _keys(expression, {"op", "condition", "then", "else"}, "if expression")
        branch = "then" if _boolean(_expression(expression["condition"], context)) else "else"
        return _expression(expression[branch], context)
    raise RuleEvaluationError("unknown expression operation")


def _evaluate(
    program: object,
    context: object,
    *,
    threshold: object,
    naive: bool,
) -> dict[str, object]:
    document = _object(program, "program")
    variables = _object(context, "context")
    _keys(document, {"rules"}, "program")
    if not isinstance(document["rules"], list):
        raise RuleEvaluationError("program rules must be a list")
    limit = _integer(threshold)

    total = 0
    matched: list[str] = []
    labels: list[str] = []
    trace: list[dict[str, object]] = []
    for raw_rule in document["rules"]:
        rule = _object(raw_rule, "rule")
        _keys(rule, {"name", "when", "score", "labels"}, "rule")
        name = rule["name"]
        rule_labels = rule["labels"]
        if (
            not isinstance(name, str)
            or not isinstance(rule_labels, list)
            or not all(isinstance(label, str) for label in rule_labels)
        ):
            raise RuleEvaluationError("rule name and labels must be strings")

        eager_score = _integer(_expression(rule["score"], variables)) if naive else None
        condition = _boolean(_expression(rule["when"], variables))
        contribution = 0
        if condition:
            contribution = (
                eager_score
                if eager_score is not None
                else _integer(_expression(rule["score"], variables))
            )
            total += contribution
            matched.append(name)
            labels.extend(rule_labels)
        trace.append({"rule": name, "matched": condition, "score": contribution})
        if naive and condition:
            break

    allowed = total > limit if naive else total >= limit
    return {
        "decision": "allow" if allowed else "deny",
        "total": total,
        "matched": matched,
        "labels": labels,
        "trace": trace,
    }


def correct_port(
    program: object,
    context: object,
    *,
    threshold: object,
) -> dict[str, object]:
    """Preserve short-circuiting, all-match accumulation and inclusive thresholds."""

    return _evaluate(program, context, threshold=threshold, naive=False)


def naive_port(
    program: object,
    context: object,
    *,
    threshold: object,
) -> dict[str, object]:
    """Inject eager evaluation, first-match and exclusive-threshold defects."""

    return _evaluate(program, context, threshold=threshold, naive=True)
