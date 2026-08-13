"""Programmatic Hypothesis shrinking for minimal dataframe counterexamples."""

from __future__ import annotations

import random
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from hypothesis import HealthCheck, Phase, find, settings
from hypothesis.errors import NoSuchExample, Unsatisfiable

from parity.generation import bundle_strategy, frame_strategy
from parity.models import FrameSchema, GenerationConfig, InputBundle


@dataclass(frozen=True, slots=True)
class Counterexample:
    """A minimal failing input discovered and shrunk by Hypothesis."""

    example: pa.Table
    source: str = "generated:shrunk"

    @property
    def table(self) -> pa.Table:
        return self.example


@dataclass(frozen=True, slots=True)
class InputBundleCounterexample:
    """A jointly minimized, atomically replayable input bundle."""

    example: dict[str, pa.Table]
    source: str = "generated:shrunk"

    @property
    def tables(self) -> dict[str, pa.Table]:
        return dict(self.example)


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
    except Unsatisfiable as error:
        raise ValueError(
            "frame generation is unsatisfiable; relax row bounds, column domains, "
            "uniqueness, or frame constraints"
        ) from error
    return Counterexample(example=example)


def find_unseen_counterexample(
    schema: FrameSchema,
    classifier: Callable[[pa.Table], str | None],
    excluded_signatures: Collection[str],
    config: GenerationConfig | None = None,
) -> Counterexample | None:
    """Find a witness whose stable mismatch signature has not been excluded.

    ``classifier`` returns ``None`` for an equivalent input and a versioned
    signature for a semantic failure. Exceptions deliberately propagate so an
    operational error cannot be learned as another semantic finding. Callers
    must re-observe the returned witness before persisting it.
    """

    excluded = frozenset(excluded_signatures)
    return find_counterexample(
        schema,
        lambda table: (signature := classifier(table)) is not None and signature not in excluded,
        config,
    )


def find_bundle_counterexample(
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
    predicate: Callable[[dict[str, pa.Table]], bool],
    config: GenerationConfig | None = None,
) -> InputBundleCounterexample | None:
    """Find and jointly shrink an atomic bundle satisfying ``predicate``."""

    try:
        selected = config or GenerationConfig()
        kwargs: dict[str, Any] = {}
        if selected.seed is not None:
            kwargs["random"] = random.Random(selected.seed)
        example = find(
            bundle_strategy(bundle, schemas),
            predicate,
            settings=hypothesis_settings(selected),
            **kwargs,
        )
    except NoSuchExample:
        return None
    except Unsatisfiable as error:
        raise ValueError(
            "input bundle generation is unsatisfiable; relax input row bounds, "
            "key domains, or relationship constraints"
        ) from error
    return InputBundleCounterexample(example=dict(example))


def find_unseen_bundle_counterexample(
    bundle: InputBundle,
    schemas: Mapping[str, FrameSchema],
    classifier: Callable[[dict[str, pa.Table]], str | None],
    excluded_signatures: Collection[str],
    config: GenerationConfig | None = None,
) -> InputBundleCounterexample | None:
    """Find a jointly minimized bundle with an unexcluded mismatch signature."""

    excluded = frozenset(excluded_signatures)
    return find_bundle_counterexample(
        bundle,
        schemas,
        lambda tables: (signature := classifier(tables)) is not None and signature not in excluded,
        config,
    )


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
    "InputBundleCounterexample",
    "find_bundle_counterexample",
    "find_counterexample",
    "find_unseen_bundle_counterexample",
    "find_unseen_counterexample",
    "hypothesis_settings",
    "minimize_counterexample",
]
