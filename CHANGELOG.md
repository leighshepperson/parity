# Changelog

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
