"""Validated import-target syntax shared by configuration and execution."""

from __future__ import annotations

import re

_IDENTIFIER = r"[A-Za-z_]\w*"
IMPORT_TARGET_PATTERN = rf"^{_IDENTIFIER}(?:\.{_IDENTIFIER})*:{_IDENTIFIER}(?:\.{_IDENTIFIER})*$"
IMPORT_TARGET = re.compile(IMPORT_TARGET_PATTERN)


def is_import_target(value: str) -> bool:
    """Return whether ``value`` names dotted identifier segments on both sides."""

    return bool(IMPORT_TARGET.fullmatch(value))


__all__ = ["IMPORT_TARGET", "IMPORT_TARGET_PATTERN", "is_import_target"]
