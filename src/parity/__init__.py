"""Parity: semantic verification for dataframe migrations."""

from parity.api import check, verify
from parity.models import (
    AdapterName,
    CaseConfig,
    CaseResult,
    ComparisonPolicy,
    FrameSchema,
    ParityConfig,
    SuiteResult,
)

__all__ = [
    "AdapterName",
    "CaseConfig",
    "CaseResult",
    "ComparisonPolicy",
    "FrameSchema",
    "ParityConfig",
    "SuiteResult",
    "check",
    "verify",
]

__version__ = "0.1.0"
