# Agent migration protocol

This protocol gives a coding agent a closed, reviewable workflow for a library migration. Parity
finds behavioural differences; the migration manifest records which declared units have evidence.
Neither mechanism discovers the complete public API automatically. A person must review the initial
inventory and every exclusion before treating the gate as meaningful.

## Completion contract

A migration is complete only when:

- every declared in-scope unit has one or more mapped Parity cases and every case passed;
- at least one in-scope unit passed, so an all-excluded manifest cannot pass;
- no unit is failed, errored or uncovered;
- exclusions have a specific, reviewed reason;
- the original project test suite passes against the candidate;
- required Python and dependency-version lanes pass; and
- every retained, report-referenced counterexample reproduces in the environment that produced it.

The gate establishes this only for units present in `migration.toml`. It cannot prove that the
inventory is exhaustive or that a mapped case genuinely exercises the unit it names. Review the
manifest and wrappers as code, not as generated paperwork.

## 1. Freeze the boundary

Keep the reference implementation available until the migration is accepted. Record its release or
commit and prepare separate, pinned reference and candidate environments where their dependencies
differ. Put accepted PEP 440 ranges in each callable's `required_distributions`; use
`record_distributions` for additional provenance that should be observed but not enforced. Parity
also requires each configured worker to carry the exact controller version before it imports the
target.

Define observable boundaries rather than internal helpers. A unit can be a public function, method,
stateful lifecycle, dispatch form or serialization contract. Wrap stateful operations such as
`fit()` followed by `predict()` in one importable callable that returns a stable tabular or JSON-like
observation.

## 2. Inventory before implementation

Review public exports, API documentation, type stubs, supported signatures and upstream tests. Add
every intended unit to `migration.toml` before writing the candidate:

```toml
version = 1

[[units]]
id = "augment-lags"
cases = ["lags-control", "lags-null-date", "lags-duplicates"]

[[units]]
id = "augment-ewm"

[[units]]
id = "plot-timeseries"
excluded_reason = "Presentation-only figure output is outside this migration."
```

`augment-ewm` is deliberately uncovered and keeps the initial gate red. `cases` and
`excluded_reason` are mutually exclusive. Split a partly excluded API into smaller reviewable units
instead of excluding covered and uncovered behaviour together.

Run the initial gate:

```bash
parity migration check \
  --manifest migrations/migration.toml \
  --config migrations/parity.toml \
  --json .parity/migration-status.json
```

An uncovered unit exits `1`. An unknown case name or invalid manifest exits `2` before project
callables execute. The gate runs the union of mapped cases once, even when several units share a
case, and attempts every mapped case rather than honouring `fail_fast`.

For a managed environment matrix, declare the workspace only after the candidate checkout,
`parity.toml` and `migration.toml` exist:

```bash
python -m pip install "parity-check[workspace]"
parity migration init \
  --reference 'your-library==1.2.3' \
  --candidate . \
  --lane minimum=requirements/minimum.txt \
  --lane current=requirements/current.txt
```

This writes `parity.workspace.toml`. It does not clone or select source, apply the migration patch,
or modify the checkout. Source preparation remains a separate, reviewable agent action.

## 3. Build evidence one unit at a time

For each unit:

1. Write pure reference and candidate wrappers at a meaningful public boundary.
2. Add a small, non-sensitive fixture with representative structure.
3. Define row order, column order, dtypes, missing-value semantics and numerical tolerances from the
   actual consumer contract.
4. Add a finite passing control before nullable, duplicate, empty, temporal and generated cases.
5. Implement or repair the candidate.
6. Run the focused cases, replay every finding and promote a sanitized witness into the project's
   regression tests.
7. Add case names to the unit only when the cases exist and exercise that unit.

Use ordinary Parity commands during the inner loop:

```bash
parity doctor --config migrations/parity.toml
parity check \
  --config migrations/parity.toml \
  --case lags-null-date \
  --no-performance \
  --json .parity/lags-null-date.json
parity replay .parity/lags-null-date/<finding-directory>
```

When a suite or migration JSON report references several retained findings, batch the same check:

```bash
parity evidence verify .parity/migration-status.json \
  --json .parity/evidence-status.json
```

Exit `0` means every artifact reproduced its expected mismatch shape, `1` means at least one is
stale, and `2` means verification errored. Treat `ms1:...` as a data-free classifier, not a digital
signature or proof of source identity. This command re-executes project code; do not run an
unreviewed checkout or artifact outside a sandbox.

Classify each mismatch as a candidate defect, reference defect, intentional contract change,
invalid generated domain or unresolved decision. Never make a run green by widening tolerances,
ignoring order or dtypes, removing hostile inputs, reducing the domain, or converting an in-scope
unit into an exclusion without an explicit reviewed decision.

## 4. Exercise supported environments

Run the original upstream tests and focused Parity cases against the supported dependency floor and
current supported releases. For a version migration, use the same wrapper in separate Python
environments. For an implementation migration, keep fixtures and comparison policy identical across
the reference and candidate.

Record dependency versions rather than relying on an ambient environment. A passing current stack
does not establish compatibility with a declared older stack.

With `parity.workspace.toml`, use one command for the complete matrix:

```bash
parity migration run
```

Parity resolves a hash-pinned requirements lock, prepares isolated reference/candidate workers and
writes one migration report per lane. tox, tox-uv and uv are hidden lifecycle/resolver details.
Re-running retains the selected lock; use `--refresh-locks` only as an intentional dependency
change. Explicit `reference.python` and `candidate.python` configs remain valid when the project or
CI platform provisions environments itself.

## 5. Enforce completion

Run the project suite, then the unfiltered migration gate with the performance policy committed in
`parity.toml`. For a managed workspace:

```bash
pytest
parity migration run
```

Verify evidence only for reports containing retained failures; a passing report intentionally has
no counterexample artifacts:

```bash
parity evidence verify path/to/retained-failure-report.json
```

For externally managed interpreters, use `parity doctor --config`
followed by `parity migration check --json ...` and run `parity evidence verify` on any report with
retained findings.

Do not substitute a collection of successful `parity check --case ...` commands for the final
gate. The migration command intentionally has no case, tag, generation-budget or performance
override: a partial or weakened run cannot certify the ledger.

Exit codes are:

- `0`: at least one declared unit passed and every other unit passed or was explicitly excluded;
- `1`: a unit failed or remained uncovered, or every unit was excluded; and
- `2`: the manifest/configuration was invalid or execution evidence was missing, skipped,
  unexercised or errored.

The JSON report uses migration report schema version 1. It contains the derived unit summary, a
canonical manifest hash and the existing data-safe Parity report for the mapped-case union, whose
provenance includes the effective configuration hash. Unit IDs, case names and exclusion reasons
are passed through report redaction. Reports omit compared values, but counterexample artifacts
contain fixture-derived or generated input data and require restricted storage.

## Agent rules

- Preserve the reference until final acceptance.
- Do not invent unsupported behaviour or silently narrow the migration scope.
- Do not mark an item complete from source inspection alone; require executable evidence.
- Do not weaken an equivalence policy solely because it reports a difference.
- Do not treat matching exceptions as proof of a successful business result.
- Do not publish raw counterexamples or production-shaped fixtures.
- Do not let environment tooling clone, patch or otherwise obscure the reviewed candidate source.
- Do not accept `ms1:` mismatch classifiers as cryptographic signatures or attestations.
- Do not declare completion while any unit is failed, errored or uncovered.
- State the inventory limitation when reporting completion: all **declared** in-scope units passed.
