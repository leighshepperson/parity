# Changelog

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

- Add `parity migration init/setup/run`, an optional managed workspace that uses tox, tox-uv and uv
  to prepare locked reference/candidate workers and run every dependency lane.
- Add `cases_file`, bounded `case_defaults`, and side-specific `reference_kwargs` /
  `candidate_kwargs` for concise migration suites without hiding case identity or input contracts.
- Add fail-closed `required_distributions` checks and exact worker/controller Parity agreement before
  target import or invocation.
- Add `parity evidence verify` for integrity checking and batch replay of findings referenced by
  suite or migration reports, with data-safe output and stable `0`/`1`/`2` exits.
- Require complete runtime provenance and an effective-configuration hash for automatic replay.
- Record input-mutation evidence as ordered labels in `Observation.mutated_inputs`.
- Make the composite Action install its selected source revision directly. The moving `v0` tag
  tracks the latest final 0.x release; minor releases may change public contracts before 1.0.
