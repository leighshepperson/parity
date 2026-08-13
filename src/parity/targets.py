"""Validated import-target syntax shared by configuration and execution."""

from __future__ import annotations

import re

# Kept as a coarse exported pattern. Python's complete identifier grammar is
# Unicode-aware and cannot be represented faithfully by :mod:`re`; validation
# must use ``is_import_target``.
IMPORT_TARGET_PATTERN = r"^[^:\s]+:[^:\s]+$"
IMPORT_TARGET = re.compile(IMPORT_TARGET_PATTERN)


def is_import_target(value: str) -> bool:
    """Return whether ``value`` names dotted identifier segments on both sides."""

    if not isinstance(value, str) or value.count(":") != 1:
        return False
    module_path, attribute_path = value.split(":", 1)
    return all(
        segment.isidentifier()
        for path in (module_path, attribute_path)
        for segment in path.split(".")
    )


__all__ = ["IMPORT_TARGET", "IMPORT_TARGET_PATTERN", "is_import_target"]
