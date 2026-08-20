# Changelog

## 0.14.1

### Migration workflow UX

- Keep generated migration contracts in `needs_review` until the scaffolded
  `NotImplementedError` adapter has actually been replaced, even when every checklist item is
  marked resolved. Validate the generated adapter without importing or executing project code.
- Disable benchmarking in generated project and migration configs until users opt in after
  semantic compatibility passes. When enabled benchmarking cannot run, report a safe actionable
  cause and the `--no-performance` recovery path instead of a generic measurement error.
- Make emitted report, workspace log, source-provenance and finding-artifact paths directly usable
  from the invocation directory, including parent and unrelated working directories.
- Print the runnable `parity check` command after `parity init`.

## 0.14.0

### Agent-first migration workflow

- Add `migration init --scaffold --json`, which creates a deliberately incomplete Arrow adapter,
  fixture, config, ledger, workspace and four-item review checklist without overwriting authored
  files. Add the no-execution `migration validate` gate; unresolved review exits `1`, invalid
  contracts exit `2`, and validation never creates managed environments or invokes target code.
- Add one-document JSON stdout contracts to `migration init`, `migration validate`, `migration run`
  and `replay`, including typed checks/issues, report/artifact references, safe result projections
  and next commands as argv arrays. Add discoverable Draft 2020-12 schemas through
  `parity schema list` and `parity schema NAME`. The versioned schemas are frozen package resources,
  so their bytes do not change with the installed Pydantic version, and fully describe config cases,
  suite cases/findings and migration units.
- Replace workspace v2 with breaking workspace v3, which can bind the generated checklist. Older
  workspace documents are intentionally rejected rather than upgraded implicitly.

### Portable replay and lean installation

- Replace manifest/replay v1 with breaking v2 contracts. Artifacts store a bounded ancestor anchor
  to their project base; replay resolves exclusively from the artifact and works independently of
  the caller's current directory. Invalid, excessive and escaping anchors fail closed, and v1 is
  intentionally rejected.
- Remove pandas and Polars from core dependencies, load their adapters lazily, and provide explicit
  `parity-check[pandas]` / `parity-check[polars]` install guidance. The zero-option starter is now
  Arrow-only, while test/development extras retain both engines.
- Run tox and uv through the controller interpreter instead of resolving console scripts from
  `PATH`, keeping managed setup deterministic from absolute virtual-environment entry points.

## 0.13.0

### Managed migration workflow

- Replace the asymmetric managed-workspace format with the breaking version 2 schema. Either side
  may be an exact released requirement or an existing local checkout, declared with exactly one of
  `reference_package` / `reference_path` and exactly one of `candidate_package` /
  `candidate_path`. The matching CLI flags are `--reference-package`, `--reference-path`,
  `--candidate-package` and `--candidate-path`.
- Let `parity migration init` create the first fixture-backed `parity.toml` and migration ledger
  with `--target` (or side-specific targets) plus `--fixture`. Existing reviewed case contracts are
  never overwritten, including when `--force` replaces only the workspace declaration.
- Bind exact released versions on both target sides, reuse separately hash-pinned locks by default,
  and retain `--refresh-locks` as the deliberate dependency-update operation. Every local checkout
  is installed editable, verified and reported with a path-free Git/content identity that replay
  binds. Local/local runs additionally retain paired driver snapshots and a two-source
  `source-provenance.json` report.

### CLI and replay

- Add the conventional `parity --version` spelling while retaining `parity version`.
- Base configured artifact paths on the directory containing the loaded `parity.toml`; managed
  workspaces reject a config directory that does not contain their workspace and generated
  environments, keeping automatic replay paths configuration-local. Retain an
  optional per-side `replay_blockers` map with bounded `live_callable`, `external_python`,
  `external_workdir`, `external_command` or `missing_command` reason codes when automatic replay is
  unavailable. Replay errors identify the affected side and the configuration-local repair.

## 0.12.0

### Behaviour and findings

- Model every target invocation as a first-class semantic `Return(canonical_value)` or
  `Raise(exception_type, normalized_message, structured_details)` outcome. Return-versus-raise and
  meaningfully different raises are behavioural incompatibilities.
- Normalize exception messages and allow-listed validation details into privacy-safe fingerprints,
  separating unrelated numerical, API and validation regressions while volatile paths, addresses,
  timestamps, IDs, versions and witness literals do not destabilize deduplication.
- Report safe structured reference/candidate exception outcomes with well-known qualified types,
  allow-listed Pydantic error codes/location shapes and NumPy API tokens. Custom identifier-shaped
  metadata remains opaque. Terminal findings print the complete `ms3:` replay signature without
  exposing raw messages or witness values.
- Reserve `ERROR` for failures that prevent a meaningful comparison: transport/import/adapter/
  canonicalisation failure, invalid protocol, timeout or worker crash. A target-raised exception,
  including `TimeoutError`, remains semantic evidence and produces `FAILED` when the sides differ.
- Generate `ms3:` mismatch-shape fingerprints with ordered Return/Raise state, qualified exception
  type and normalized exception semantics.
- Surface data-safe mismatch summaries and paths in human reports while preserving exact evidence
  in private replay artifacts.

### Decoupled targets

- Replace full target-side Parity workers with a dependency-light portable Python worker. Isolated
  Python environments now need PyArrow, their selected adapter dependency and the application under
  test—not Parity, Pydantic, Hypothesis, Rich or Typer.
- Add two-phase target preflight: validate both transports/runtime identities and declared
  requirements before importing either endpoint, then validate target, adapter and
  output-canonicalizer imports without invoking application code. When one transport fails, the
  deferred peer endpoint is explicitly `not_checked` with `TargetEndpointNotChecked`, rather than
  ambiguously ready or failed.
- Add first-class external command targets over target protocol v1. Any local executable can serve
  as a reference or candidate through private Arrow/JSON call directories and strict
  Return/Raise/Error observations.
- Keep command credentials out of reports and replay contracts, reject automatic replay when an
  executable contract had to be redacted, and require replayed command paths to remain inside the
  original invocation project.
- Bound every command-protocol response/output read and reject redirects, hard links, non-regular
  files, replacement and concurrent mutation at that artifact boundary.
- Add optional per-side output canonicalizers so APIs may return different domain objects while
  preserving a small shared behavioural contract.
- Preserve generic runtime and path-free source identity for Python and non-Python targets, and bind
  those identities into replay.

### Generation, scheduling and performance

- Add first-class project generators through `generation.generator`. Hypothesis strategies retain
  shrinking; bounded iterables reuse deterministic domain corpora without growing the TOML schema
  into a proprietary data language.
- Add full-match regular expressions, string-length bounds and IANA time-zone-aware datetime
  generation, including applicable daylight-saving gaps, folds and transition boundaries.
- Raise the default distinct-finding budget from one to ten so unrelated incompatibilities are
  normally separated without requiring a discovery-specific configuration override.
- Add `parity check --jobs N` and top-level `jobs` for ordered case-level concurrency. Each case owns
  isolated sessions and artifact paths; discovery/shrinking inside a case remains serial.
- Add `--native-threads N` and callable-level native thread limits for common BLAS/OpenMP pools,
  preventing case concurrency from multiplying target-side thread pools.
- Replace point-estimate performance gates with deterministic paired-bootstrap confidence
  intervals. Gates require enough repeats and fail only when the interval's lower bound exceeds the
  configured speed or memory threshold.
- Fail with infrastructure `ERROR` when enabled performance measurement lacks a validated passing
  input or cannot complete, instead of silently omitting the requested evidence.

### Migration workflow and documentation

- Support released-versus-local and local-versus-local managed workspaces with independently locked
  environments. Local pairs verify editable import origins, record path-free Git/content provenance
  and fail closed if either checkout changes during a run.
- Document one `parity check`-centred workflow, changed-API adapters, custom generators,
  branch/worktree regression testing, rolling adjacent migrations, performance uncertainty and the
  external target protocol.
- Reframe Parity as a general open-source behavioural compatibility engine. Code generation and
  migration repair remain intentionally outside the core; AI tools may consume Parity as a
  deterministic verification layer.

## 0.11.0

- Make managed migrations one explicit active adjacent pair. Add `parity migration advance`,
  preserve reusable core checks and lane locks, and invalidate old active reports without building
  a migration-history store.
- Bind the managed subject distribution automatically: enforce the exact released reference,
  record both sides, use a non-shadowing harness workdir and reject candidate-source exposure before
  target import.
- Make `migration init` use a contained `migrations/` harness by default, rebase invocation paths
  correctly and create a reviewable `core-regression` starter ledger when one is absent.
- Compare stability repeats with exact reflexive identity so null/NaN policies and invalid row-key
  contracts are not misreported as nondeterminism; reject explicit empty Python/pytest selections.
- Add data-safe evidence reason codes, nested case evidence in migration output, consistent atomic
  report writing, private-state self-ignore files and traceback-free operational write errors.
- Keep Action-major promotion exclusively in the successful release path, with manual recovery
  available separately, and rewrite current migration documentation around the streamlined flow.

## 0.10.0

- Add `parity migration init/setup/run`, an optional managed workspace that prepares separately
  locked reference/candidate environments and runs every dependency lane.
- Add `cases_file`, bounded `case_defaults`, and side-specific `reference_kwargs` /
  `candidate_kwargs` for concise migration suites without hiding case identity or input contracts.
- Add fail-closed `required_distributions` checks and runtime provenance before target import or
  invocation.
- Add `parity evidence verify` for integrity checking and batch replay of findings referenced by
  suite or migration reports, with data-safe output and stable `0`/`1`/`2` exits.
- Require complete runtime provenance and an effective-configuration hash for automatic replay.
- Record input-mutation evidence as ordered labels in `Observation.mutated_inputs`.
- Make the composite Action install its selected source revision directly. The moving `v0` tag
  tracks the latest final 0.x release; minor releases may change public contracts before 1.0.
