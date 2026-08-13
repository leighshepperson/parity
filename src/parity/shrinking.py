"""Programmatic Hypothesis shrinking for minimal dataframe counterexamples."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from hypothesis import HealthCheck, Phase, find, settings
from hypothesis.errors import NoSuchExample

from parity.generation import frame_strategy
from parity.models import FrameSchema, GenerationConfig


@dataclass(frozen=True, slots=True)
class Counterexample:
    """A minimal failing input discovered and shrunk by Hypothesis."""

    example: pa.Table
    source: str = "generated:shrunk"

    @property
    def table(self) -> pa.Table:
        return self.example


def hypothesis_settings(config: GenerationConfig | None = None) -> settings:
    """Translate Parity generation limits into public Hypothesis settings."""

    selected = config or GenerationConfig()
    phases = (Phase.generate, Phase.shrink) if selected.shrink else (Phase.generate,)
    suppress = (
        (HealthCheck.too_slow, HealthCheck.filter_too_much) if selected.suppress_too_slow else ()
    )
    return settings(
        max_examples=selected.max_examples,
        deadline=selected.deadline_ms,
        derandomize=selected.derandomize,
        phases=phases,
        suppress_health_check=suppress,
        database=None,
    )


def find_counterexample(
    schema: FrameSchema,
    predicate: Callable[[pa.Table], bool],
    config: GenerationConfig | None = None,
) -> Counterexample | None:
    """Find the minimal table for which ``predicate(table)`` is true.

    ``None`` means the configured search budget found no failing input.  A
    predicate exception is not swallowed: callers should decide explicitly
    whether an execution crash is itself the property being searched for.
    """

    try:
        selected = config or GenerationConfig()
        kwargs: dict[str, Any] = {}
        if selected.seed is not None:
            kwargs["random"] = random.Random(selected.seed)
        example = find(
            frame_strategy(schema),
            predicate,
            settings=hypothesis_settings(selected),
            **kwargs,
        )
    except NoSuchExample:
        return None
    return Counterexample(example=example)


def minimize_counterexample(
    initial: pa.Table,
    predicate: Callable[[pa.Table], bool],
    *,
    config: GenerationConfig | None = None,
) -> Counterexample | None:
    """Shrink within a schema inferred from an existing failing table.

    The initial example supplies domains and upper row count.  The search still
    verifies the predicate and returns ``None`` when no generated example fails.
    """

    from parity.schema import infer_schema

    if not predicate(initial):
        raise ValueError("initial example does not satisfy the failure predicate")
    schema = infer_schema(initial, min_rows=0, max_rows=max(initial.num_rows, 1))
    return find_counterexample(schema, predicate, config)


__all__ = [
    "Counterexample",
    "find_counterexample",
    "hypothesis_settings",
    "minimize_counterexample",
]
