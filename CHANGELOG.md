# Changelog

## 0.10.0

- Add an optional managed migration workspace: `parity migration init/setup/run` uses tox,
  tox-uv and uv behind the CLI to resolve hash-pinned locks, prepare isolated reference/candidate
  workers and run every declared dependency lane with its own migration report.
- Add `cases_file` and bounded `case_defaults` loader syntax for reusable large migration suites.
  Defaults can cover callable environment policy, comparison, generation, performance,
  side-specific keywords and timeouts without hiding case identity, targets or input contracts.
- Add `required_distributions` with normalized PEP 440 ranges. Configured workers now fail closed
  before target import/invocation when a required package is missing or incompatible, or when the
  worker and controller carry different Parity versions.
- Add `reference_kwargs` and `candidate_kwargs` so otherwise identical wrappers can receive
  endpoint-specific switches while shared keyword precedence remains unambiguous and replayable.
- Add `parity evidence verify` to integrity-check and replay every mismatch-classified artifact
  referenced by a suite or migration JSON report, distinguishing reproduced, stale and errored
  evidence with stable `0`/`1`/`2` exits and an optional data-safe JSON report.
- Publish current Action documentation through the stable moving `v0` channel and promote that
  channel only after its matching package release succeeds. Controller-version examples derive the
  installed version instead of copying a patch number into worker setup commands.

### Compatibility notes

- The workspace is an optional `parity-check[workspace]` install. It consumes an exact released
  reference requirement and an existing local candidate checkout; it never clones, patches or
  modifies source. Managed setup requires a static candidate distribution name matching the
  reference; dynamic legacy metadata can continue to use explicitly provisioned worker Python
  paths.
- Generated requirements locks, environments and tox configuration live under
  `.parity/workspace`. Existing locks keep dependency selection stable; pass `--refresh-locks` only
  to request a deliberate dependency upgrade.
- The `parity.toml` format remains version 1. Inline `[[cases]]` remains supported; a root document
  must choose exactly one of inline cases or `cases_file`. The expanded effective configuration is
  validated and hashed normally.
- Required package ranges are opt-in, but exact worker/controller Parity agreement is now mandatory
  for configured execution. Install the same `parity-check` release in externally managed workers.
- Evidence report schema 1 is new and data-safe. Its `ms1:...` mismatch value is a classifier, not a
  cryptographic signature, source attestation or trust decision; verification executes trusted
  project code and does not turn process isolation into a sandbox.

## 0.9.2

- Keep installed reference and candidate worker environments isolated. A wheel-installed
  orchestrator no longer prepends its whole `site-packages` directory to configured workers and
  can no longer mask dependency-version differences across virtual environments.
- Add a five-API, two-dependency-lane PyTimeTK migration pilot that exercises the migration ledger,
  records baseline incompatibilities, applies a source-level repair and verifies version drift.
- Add `generation.search = false` for exact fixture-only campaigns while rejecting searchless
  configurations that would otherwise execute no inputs.

### Compatibility notes

- Source checkouts still inject their narrow `src` root so development workers can import Parity.
  Installed workers must carry their own `parity-check` installation; a missing installation now
  fails closed instead of borrowing the orchestrator environment.

## 0.9.1

- Add a strict version 1 migration manifest and `parity migration check` coverage gate that maps
  declared migration units to configured cases, explicit exclusions or visible uncovered work.
- Run the complete mapped-case union once, reject unknown mappings before execution and require at
  least one passing unit so an all-excluded inventory cannot pass.
- Add a data-safe migration JSON report bound to canonical manifest and effective Parity
  configuration hashes, plus an agent-oriented inventory, implementation, replay and release
  workflow.
- Supersede the non-publishing `v0.9.0` tag, which pointed at package version 0.8.1 and therefore
  failed the release version guard. Version 0.9.1 is the first distributable with the migration
  ledger.

### Compatibility notes

- The migration manifest and its report are new, independently versioned contracts. Existing
  `parity.toml`, suite report, replay and counterexample formats are unchanged.
- Migration exit `0` means at least one declared unit passed and every other unit passed or was
  explicitly excluded. Failed, uncovered and all-excluded manifests return `1`; invalid or
  incomplete execution evidence returns `2`.
- Migration reports redact unit IDs, case names and exclusion reasons and omit compared values.
  Replay artifacts retain actual inputs and require the same restricted handling as ordinary runs.

## 0.8.1

- Compare finite integers, Decimals and mixed numeric types without first converting them to
  binary floats. Exact comparisons now distinguish adjacent integers above `2**53`, high-precision
  Decimals and finite Decimals outside the float range.
- Apply existing relative and absolute tolerances with exact rational arithmetic for ordinary
  numeric ranges and bounded, directed Decimal intervals for extreme exponents.
- Preserve wider-than-binary64 NumPy floating scalars instead of recursively unboxing or narrowing
  them before comparison.
- Add scalar, frame, keyed, order-insensitive, isolated-worker, replay and synthetic fault-corpus
  regressions for numeric precision loss.

### Compatibility notes

- Configuration, artifact, replay and report formats are unchanged. Python and binary16/32/64
  float comparison retains `math.isclose` behaviour, including NaN, infinity and signed-zero
  policy handling; wider NumPy floats use their exact ratios.
- With `rtol = 0` and `atol = 0`, mismatches previously hidden by float coercion are now reported.
  Nonzero tolerances retain the same symmetric relative/absolute rule but apply it to exact values.

## 0.8.0

- Add a fixture-backed `parity init` mode for existing reference and candidate targets, including
  adapters, worker interpreters, explicit distribution provenance and keyed output alignment.
- Add `parity doctor --config` to inspect both workers without importing or invoking either target.
  It reports only path-free Python, Parity and explicitly requested distribution versions.
- Preserve configured virtual-environment Python entry points instead of dereferencing them to a
  shared base interpreter, so side-specific dependency versions remain observable and replayable.
- Validate import targets as dotted Python identifiers before writing or running a project config.
- Document a small two-environment dependency-version workflow. Environment creation and package
  installation remain explicit user steps rather than a Parity-managed environment system.

### Compatibility notes

- Plain `parity init` still produces the same editable starter and demo module. Project mode is
  selected only when `--reference`, `--candidate` and `--fixture` are supplied together.
- Plain `parity doctor` and its JSON payload retain their existing local dependency report.
  Configured doctor output is a separate data-safe contract and exits with status 2 when a worker
  cannot start or an explicitly recorded distribution is missing.
- Configuration, replay, artifact and report format versions are unchanged.
- Replay accepts a project-local virtual-environment entry point whose symlink target is a host
  Python binary. This narrowly scoped interpreter rule does not weaken resolved containment for
  workdirs, manifests or artifact inputs; replay remains execution of trusted project code.

## 0.7.0

- Add keyed output alignment with unique scalar composite keys. Reordered outputs can now be
  matched by business identity while payload differences retain precise cell-level evidence.
- Replace greedy order-insensitive row matching with deterministic maximum-cardinality matching,
  removing false failures when numeric tolerance admits more than one possible pairing.
- Keep row-key identity exact: value and datetime tolerances apply to payloads, never to keys;
  missing-value and signed-zero key identity follow the explicit comparison policy.
- Add a pinned public compatibility study whose grouped output uses composite keys.

### Compatibility notes

- Existing `strict` and `ignore` row-order configurations retain their meaning. `row_keys` must be
  omitted or empty unless `row_order = "keyed"`.
- Keyed comparison rejects duplicate, non-scalar or policy-non-reflexive keys rather than choosing
  an arbitrary row pairing.
- Replay and report format versions are unchanged; the additive comparison fields are already
  covered by the versioned configuration fingerprint. Older policies still deserialize with an
  empty `row_keys` default, but exact replay of version 2 and 3 artifacts continues to require the
  recorded Parity and worker runtimes.

## 0.6.0

- Add `generation.stability_repeats`, defaulting to two same-input observations per implementation.
  A matching but unstable pair now stops as an unsigned execution error before generated search or
  benchmarking; setting the value to `1` explicitly disables the gate.
- Add declarative `sorted_by` and `row_comparison` frame constraints. Deterministic cases,
  property generation, shrinking and multi-input relationship rewrites preserve the declared valid
  domain.
- Add CLI and composite Action overrides for stability observations.
- Add executable sorted as-of/valid-interval and hidden-state stability studies.

### Compatibility notes

- Existing schemas remain valid because frame constraints default to an empty list.
- Stability checking is intentionally stricter: a deterministic input that used to pass because
  both sides changed in the same way now returns an execution error. Set `stability_repeats = 1`
  only when repeated observation is deliberately unwanted.
- The composite Action's `stability-repeats` input and frame constraints require
  `leighshepperson/parity@v0.6.0` or later.

## 0.4.0

- Add atomic two- and three-frame input bundles for joins and lookups, with keyword or positional
  binding, per-input mutation evidence, joint shrinking and replay.
- Add relational generation constraints for key overlap, foreign keys, equal row counts and key
  cardinality.
- Add bounded multi-finding campaigns. `generation.max_findings` defaults to `1`; higher values
  continue searching for distinct, data-free mismatch signatures.
- Confirm saved findings in clean execution state and stop with an error when a witness is unstable
  or cannot be reproduced.
- Add replay contract version 3 and manifest version 2 for hash-bound multi-input artifacts. Existing
  single-input replay contracts remain supported.
- Add JSON report schema version 3 fields `finding_signature` and `findings_discovered`.
- Add a synthetic pandas `merge` / Polars `join` compatibility study.

### Compatibility notes

- Existing single-input version 1 TOML files that use non-redundant schemas continue to work without
  changes. Schema validation now rejects duplicate categories, null categories on non-nullable
  columns, and empty or duplicate `unique_together` groups instead of failing later during search.
- Consumers that validate the JSON report schema must accept schema version 3 before upgrading.
- Multi-input artifacts require Parity 0.4 or later to replay. Older single-input artifacts remain
  readable and are marked unverified when they predate runtime provenance.
- The composite Action's `max-findings` input requires `leighshepperson/parity@v0.4.0` or later.
