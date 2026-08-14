# Roadmap

Parity is an open-source, local-first verifier for dataframe and numerical migrations. This roadmap
lists technical priorities, not release dates or commitments. Work should be guided by reproducible
public examples and the risk of false passes.

## Current usability foundation

The current release makes a complete migration easier to operate without broadening Parity into a
source manager or general build system:

- an optional `parity-check[workspace]` flow creates isolated, locked reference/candidate workers
  for one or more dependency lanes behind `parity migration init/setup/run`, while
  `migration advance` moves one active adjacent pair without accumulating a history graph;
- `cases_file` and bounded `case_defaults` remove repetitive large-config boilerplate while keeping
  case identity, targets and input contracts visible;
- side-specific `reference_kwargs` and `candidate_kwargs` let one wrapper expose controlled backend
  switches without duplicating cases;
- worker Parity and target-package requirements fail closed before target execution; and
- `parity evidence verify` batch-replays mismatch-classified findings referenced by suite or
  migration reports.

The workspace consumes a released reference and an existing local candidate checkout. It does not
clone, patch or modify source. Durable core cases remain mapped in the current manifest; hop-specific
cases and reports are replaced as the baseline advances. Parity's `ms1:` value is a mismatch-shape
digest, not a cryptographic signature or attestation.

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
- Keep the migration coverage gate resistant to partial execution, missing case evidence and
  vacuous all-excluded inventories, with versioned, data-safe reports bound to the reviewed
  manifest and effective Parity configuration.
- Exercise managed workspaces across supported platforms, resolver failures and multi-lane public
  migrations while keeping explicit externally provisioned interpreter paths supported.
- Preserve evidence-verification exit semantics and distinguish stale behavioural evidence from
  corrupt, unverifiable or unauthenticated evidence.

## Broaden semantic coverage

- Cover more Arrow logical types, especially decimals, categorical values, durations and nested
  columns.
- Strengthen generation around empty inputs, duplicate and null keys, multi-column joins, timezone
  boundaries, extreme numeric values and mixed missing-value representations.
- Extend valid-domain constraints only from concrete examples, with temporal spacing and grouped
  ordering as likely follow-ups to the initial ordering and row-comparison vocabulary.
- Exercise multi-input relational campaigns on more public join and lookup implementations, and
  refine the small relationship vocabulary from reproducible examples.
- Improve mismatch-signature classification without presenting signatures as root causes or bugs.
- Add explicit comparison policies where real migrations require them while keeping strict behaviour
  as the default.
- Improve performance checks with clearer warm-up, sampling and uncertainty reporting.
- Add standard machine-readable CI output where it provides useful review annotations.

## Extend carefully

- Define a small, documented adapter interface before adding engines beyond pandas, Polars and Arrow.
- Consider optional DuckDB or Ibis support only with public compatibility fixtures and no new
  pairwise conversion paths.
- Explore stateful-sequence campaigns after their replay and shrinking contracts are well defined.
- Explore metamorphic properties and user-supplied invariants as complements to
  reference-versus-candidate comparison.
- Keep generators, comparators and artifact readers independently testable and usable from Python.

## Before 1.0

- Version and document the configuration, migration-manifest, suite-report, migration-report and
  counterexample-manifest formats.
- Finalize the public contracts that will receive 1.x stability guarantees.
- Keep unsupported contract errors explicit and actionable.
- Publish a supported platform matrix and reproducible release process.
- Demonstrate the verifier on several independent public projects without project-specific engine
  changes.

## Possible later experiments

After the core is dependable, it may be useful to investigate stateful transformations, numerical
invariants, comparisons across languages or hardware, and signed provenance for evidence exchanged
between systems. These are exploratory ideas. They should become project work only when a concrete
public use case shows that they belong in Parity rather than another tool.

## Non-goals

- Claiming that property-based comparison is a formal proof.
- Running hostile code as a security sandbox.
- Uploading source, frames or artifacts to a hosted service by default.
- Becoming a dataframe engine, application runtime or dashboard framework.
- Adding adapters faster than they can be tested and maintained.
- Claiming that a migration manifest automatically discovers an exhaustive public API or proves
  that a mapped case exercises the unit it names.

## Choosing priorities

A proposal is strongest when it includes a public or synthetic reference/candidate pair, a semantic
difference that existing tests miss and a clear explanation of why the fix belongs in the shared
verifier. A small, well-evidenced improvement should take priority over a broad speculative feature.
