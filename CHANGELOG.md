# Changelog

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
