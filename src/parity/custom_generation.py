"""Small extension boundary for project-owned input generation.

Custom generators run in the Parity driver, not in either target environment.
They may return a Hypothesis strategy, preserving Hypothesis shrinking, or a
plain iterable which Parity consumes up to the configured example budget.
"""

from __future__ import annotations

import importlib
import keyword
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, TypeAlias

import pyarrow as pa
from hypothesis.strategies import SearchStrategy

from parity.adapters import to_arrow
from parity.execution import redact_text

GeneratedInput: TypeAlias = pa.Table | dict[str, pa.Table]

_IMPORT_LOCK = threading.RLock()


class CustomGenerationError(ValueError):
    """Raised when a project generator cannot supply a valid input stream."""


@dataclass(frozen=True, slots=True)
class CustomGenerator:
    """One validated project generator implementation."""

    strategy: SearchStrategy[GeneratedInput] | None = None
    examples: tuple[GeneratedInput, ...] = ()

    @property
    def uses_hypothesis(self) -> bool:
        return self.strategy is not None


@contextmanager
def _import_root(path: Path | None) -> Iterator[None]:
    """Temporarily expose the configuration directory for a trusted import."""

    if path is None:
        yield
        return
    root = str(path.resolve())
    with _IMPORT_LOCK:
        inserted = root not in sys.path
        if inserted:
            sys.path.insert(0, root)
        try:
            yield
        finally:
            if inserted:
                with suppress(ValueError):  # defensive against user import hooks
                    sys.path.remove(root)


def _import_factory(target: str, base_directory: Path | None) -> Any:
    module_name, attribute_path = target.split(":", 1)
    try:
        with _import_root(base_directory):
            value: Any = importlib.import_module(module_name)
            for part in attribute_path.split("."):
                value = getattr(value, part)
    except Exception as error:
        raise CustomGenerationError(
            f"could not import generator {target!r} "
            f"({type(error).__name__}: {redact_text(str(error))})"
        ) from error
    if not callable(value):
        raise CustomGenerationError(f"generator target {target!r} is not callable")
    return value


def normalize_generated_input(value: Any) -> GeneratedInput:
    """Convert one custom value into Parity's canonical Arrow input transport."""

    if isinstance(value, Mapping):
        items = list(value.items())
        if not 2 <= len(items) <= 3:
            raise CustomGenerationError(
                "a custom named input bundle must contain two or three dataframes"
            )
        converted: dict[str, pa.Table] = {}
        for name, frame in items:
            if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
                raise CustomGenerationError(
                    "custom input bundle names must be valid Python identifiers"
                )
            try:
                converted[name] = to_arrow(frame).combine_chunks()
            except Exception as error:
                raise CustomGenerationError(
                    f"custom input {name!r} is not a supported dataframe"
                ) from error
        return converted
    try:
        return to_arrow(value).combine_chunks()
    except Exception as error:
        raise CustomGenerationError(
            "custom generator values must be a supported dataframe or a mapping "
            "of two or three named dataframes"
        ) from error


def load_custom_generator(
    target: str,
    *,
    base_directory: Path | None,
    max_examples: int,
) -> CustomGenerator:
    """Load one trusted project generator and bound any plain iterable."""

    factory = _import_factory(target, base_directory)
    try:
        with _import_root(base_directory):
            produced = factory()
    except Exception as error:
        raise CustomGenerationError(
            f"generator {target!r} could not be created "
            f"({type(error).__name__}: {redact_text(str(error))})"
        ) from error

    if isinstance(produced, SearchStrategy):
        strategy: SearchStrategy[GeneratedInput] = produced.map(normalize_generated_input)
        return CustomGenerator(strategy=strategy)

    if isinstance(produced, (str, bytes, bytearray, Mapping, pa.Table)):
        raise CustomGenerationError(
            "a plain custom generator must return an iterable of input examples"
        )
    if not isinstance(produced, Iterable):
        raise CustomGenerationError(
            "custom generator must return a Hypothesis SearchStrategy or an iterable"
        )
    try:
        with _import_root(base_directory):
            examples = tuple(
                normalize_generated_input(value) for value in islice(produced, max_examples)
            )
    except CustomGenerationError:
        raise
    except Exception as error:
        raise CustomGenerationError(
            f"custom generator {target!r} could not produce examples "
            f"({type(error).__name__}: {redact_text(str(error))})"
        ) from error
    if not examples:
        raise CustomGenerationError("custom generator produced no examples")
    return CustomGenerator(examples=examples)


__all__ = [
    "CustomGenerationError",
    "CustomGenerator",
    "GeneratedInput",
    "load_custom_generator",
    "normalize_generated_input",
]
