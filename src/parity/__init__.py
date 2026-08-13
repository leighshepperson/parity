"""Parity: semantic verification for dataframe migrations."""

from parity._version import __version__
from parity.api import check, verify
from parity.models import (
    AdapterName,
    Cardinality,
    CaseConfig,
    CaseProvenance,
    CaseResult,
    ComparisonPolicy,
    EqualRowCount,
    ForeignKey,
    FrameSchema,
    InputBundle,
    InputSpec,
    KeyOverlap,
    KeyRef,
    PandasInput,
    ParityConfig,
    Relationship,
    SuiteProvenance,
    SuiteResult,
)
from parity.provenance import DistributionProvenance, RuntimeProvenance

__all__ = [
    "AdapterName",
    "Cardinality",
    "CaseConfig",
    "CaseProvenance",
    "CaseResult",
    "ComparisonPolicy",
    "DistributionProvenance",
    "EqualRowCount",
    "ForeignKey",
    "FrameSchema",
    "InputBundle",
    "InputSpec",
    "KeyOverlap",
    "KeyRef",
    "PandasInput",
    "ParityConfig",
    "Relationship",
    "RuntimeProvenance",
    "SuiteProvenance",
    "SuiteResult",
    "__version__",
    "check",
    "verify",
]
