# Roadmap

Parity is an open-source, local-first verifier for dataframe and numerical migrations. This roadmap
lists technical priorities, not release dates or commitments. Work should be guided by reproducible
public examples and the risk of false passes.

## Make the current release dependable

- Keep configuration, exit codes, reports and replay artifacts consistent across patch releases.
- Treat false passes, irreproducible counterexamples and input-isolation failures as release
  blockers.
- Improve diagnostics for schema, dtype, ordering, null, datetime, exception and tolerance
  differences.
- Test supported Python versions and operating systems, including subprocess and timeout paths.
- Add pinned, runnable case studies from public projects and retain confirmed failures as regression
  fixtures.
- Document cross-environment campaigns, redaction, artifact handling and common CI configurations.

## Broaden semantic coverage

- Cover more Arrow logical types, especially decimals, categorical values, durations and nested
  columns.
- Strengthen generation around empty inputs, duplicate and null keys, multi-column joins, timezone
  boundaries, extreme numeric values and mixed missing-value representations.
- Add explicit comparison policies where real migrations require them while keeping strict behaviour
  as the default.
- Improve performance checks with clearer warm-up, sampling and uncertainty reporting.
- Add standard machine-readable CI output where it provides useful review annotations.

## Extend carefully

- Define a small, documented adapter interface before adding engines beyond pandas, Polars and Arrow.
- Consider optional DuckDB or Ibis support only with public compatibility fixtures and no new
  pairwise conversion paths.
- Explore multiple-input and stateful-sequence campaigns after their replay and shrinking contracts
  are well defined.
- Explore metamorphic properties and user-supplied invariants as complements to
  reference-versus-candidate comparison.
- Keep generators, comparators and artifact readers independently testable and usable from Python.

## Before 1.0

- Version and document the configuration, report and counterexample-manifest formats.
- Provide a clear compatibility and deprecation policy.
- Preserve replay across supported minor versions or fail with an actionable migration message.
- Publish a supported platform matrix and reproducible release process.
- Demonstrate the verifier on several independent public projects without project-specific engine
  changes.

## Possible later experiments

After the core is dependable, it may be useful to investigate stateful transformations, numerical
invariants, comparisons across languages or hardware, and signed provenance for evidence exchanged
between systems. These are experiments, not a ten-year plan. They should become project work only
when a concrete public use case shows that they belong in Parity rather than another tool.

## Non-goals

- Claiming that property-based comparison is a formal proof.
- Running hostile code as a security sandbox.
- Uploading source, frames or artifacts to a hosted service by default.
- Becoming a dataframe engine, application runtime or dashboard framework.
- Adding adapters faster than they can be tested and maintained.

## Choosing priorities

A proposal is strongest when it includes a public or synthetic reference/candidate pair, a semantic
difference that existing tests miss and a clear explanation of why the fix belongs in the shared
verifier. A small, well-evidenced improvement should take priority over a broad speculative feature.
