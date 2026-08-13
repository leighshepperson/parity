"""Deterministic explanations for common cross-engine semantic differences."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from parity.models import Diagnosis, Mismatch, MismatchKind


def _text(mismatches: Iterable[Mismatch]) -> str:
    return " ".join(
        " ".join(
            part
            for part in (
                mismatch.message,
                mismatch.path or "",
                str(mismatch.reference or ""),
                str(mismatch.candidate or ""),
            )
            if part
        )
        for mismatch in mismatches
    ).lower()


def diagnose(mismatches: list[Mismatch]) -> list[Diagnosis]:
    """Return conservative, deduplicated hypotheses backed by mismatch evidence.

    Diagnostics never change pass/fail and deliberately avoid an LLM. They explain only patterns
    present in structured differences; the reference is not assumed to encode business intent.
    """

    if not mismatches:
        return []
    text = _text(mismatches)
    kinds = {mismatch.kind for mismatch in mismatches}
    results: list[Diagnosis] = []

    def add(
        code: str,
        title: str,
        explanation: str,
        confidence: Literal["high", "medium", "low"],
        *evidence: str,
        documentation_url: str | None = None,
    ) -> None:
        results.append(
            Diagnosis(
                code=code,
                title=title,
                explanation=explanation,
                confidence=confidence,
                evidence=list(evidence),
                documentation_url=documentation_url,
            )
        )

    if re.search(r"\b(?:null|nan|none)\b", text):
        add(
            "missing-values",
            "Missing-value semantics differ",
            "The engines may distinguish null, NaN and nullable dtypes differently. Check filters, "
            "joins, grouping keys and aggregations rather than simply widening tolerance.",
            "high",
            "The minimized difference contains a missing value.",
            documentation_url="https://docs.pola.rs/user-guide/migration/pandas/#missing-data",
        )
    if re.search(r"\b(?:timezone|timestamp|datetime|date)\b", text):
        add(
            "datetime",
            "Datetime representation or timezone semantics differ",
            "Check time units, timezone localization/conversion, daylight-saving boundaries and "
            "whether the transformation returns an instant or a wall-clock value.",
            "high",
            "A datetime-like column or value differs.",
        )
    if MismatchKind.DTYPE in kinds:
        add(
            "dtype-resolution",
            "Type resolution differs",
            "One implementation inferred or promoted a different dtype. Confirm overflow, nullable "
            "integer, string, categorical and decimal behaviour before accepting compatibility.",
            "high",
            "The outputs have different dtype families or concrete dtypes.",
        )
    if re.search(r"\b(?:row order|ordering differs|different row positions)\b", text):
        add(
            "row-order",
            "Row ordering differs",
            "A group-by, join, unique operation or query optimizer may not preserve implicit order. "
            "Add an explicit sort if order is part of the contract; otherwise configure an "
            "order-insensitive comparison with stable keys.",
            "medium",
            "The same or similar values occur at different row positions.",
        )
    if MismatchKind.ROW in kinds:
        add(
            "row-content",
            "Row content or multiplicity differs",
            "One output contains a row for which the other has no equivalent. Check filtering, "
            "grouping-key treatment, join cardinality and duplicate preservation.",
            "high",
            "An order-insensitive comparison found an unmatched row.",
        )
    if MismatchKind.COLUMN in kinds or MismatchKind.SCHEMA in kinds:
        add(
            "schema-shape",
            "Output schema differs",
            "Check index materialization, selected columns, aliases, join suffixes and aggregation "
            "column names.",
            "medium",
            "The output columns or schema are not equivalent.",
        )
    if MismatchKind.MUTATION in kinds:
        add(
            "input-mutation",
            "Input mutation differs",
            "At least one implementation modifies its input. This can change later computations "
            "even when the immediate returned value looks equivalent.",
            "high",
            "Parity observed a before/after input fingerprint change.",
        )
    if MismatchKind.EXCEPTION in kinds:
        add(
            "exception-contract",
            "Failure behaviour differs",
            "The implementations return versus raise, or raise different exception types. Decide "
            "whether invalid-input behaviour is part of the public contract.",
            "high",
            "The reference and candidate outcomes differ for the minimized input.",
        )
    if MismatchKind.VALUE in kinds and re.search(r"\b(?:float|decimal|inf|infinity)\b", text):
        add(
            "numeric-precision",
            "Numeric precision or reduction order differs",
            "Parallel reductions, dtype width and floating-point operation order can change results. "
            "Use a domain-approved tolerance, not a tolerance selected merely to make the test pass.",
            "medium",
            "A numeric output exceeds the configured tolerance.",
        )

    if not results:
        add(
            "semantic-difference",
            "Observable behaviour differs",
            "The candidate does not satisfy the configured equivalence policy for this input. The "
            "preserved counterexample is the authoritative reproduction.",
            "low",
            f"Parity recorded {len(mismatches)} structured mismatch(es).",
        )
    return results
