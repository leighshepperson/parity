# Parity

**Differential semantic testing for changed computation.**

Parity tries to disprove that a rewrite means the same thing as the implementation it replaces.
It executes both versions over fixtures and generated edge cases, compares their observable
behaviour under an explicit policy, attempts to shrink generated failures and preserves failing
inputs for replay. Performance is measured only after correctness.

The initial adapters support pandas-to-Polars comparisons. The contracts and artifact format are
engine-neutral so the same approach can cover other dataframe, numerical and dependency changes.

> Parity is an evidence generator, not a proof of mathematical equivalence. Passing means no
> difference was found within the configured domain and search budget.

## What one run gives you

- Deterministic fixture checks plus property-based exploration and shrinking.
- Cross-engine comparison of shape, columns, rows, dtypes, nulls, NaNs, signed zero, numeric
  tolerances, datetimes, returned exceptions and input mutation.
- Isolated execution with per-case timeouts and separate Python environments when configured.
- Per-worker Python, platform and dependency provenance, including explicitly named target
  distributions, so dependency drift is distinguishable from semantic drift.
- Reproducible Arrow counterexamples, plus Parquet when representable, with a manifest and replay
  command.
- Joint generation, shrinking, mutation tracking and replay for two- or three-frame joins and
  lookups.
- Exact alignment of reordered outputs by unique scalar business keys, including composite keys.
- Frame-local valid-domain constraints for sorted inputs and row-wise column relationships.
- Same-input stability checks that fail closed when either implementation changes across repeated
  observations, even when both sides happen to agree with each other.
- Bounded discovery of several distinct mismatch signatures in one campaign, with repeated
  confirmation before evidence is accepted.
- Median runtime and peak-memory comparisons with optional regression gates.
- A migration manifest that maps declared library units to cases and blocks completion when a unit
  is failing, errored or uncovered.
- Terminal, JSON, Markdown, JUnit and GitHub step-summary reporting.
- A Python API, pytest fixture, CLI and composite GitHub Action.
- Local execution. Parity has no hosted service, telemetry or required network connection.

## Install and run

Parity requires Python 3.11 or later.

```bash
python -m pip install parity-check
parity init
parity check
```

`parity init` writes a runnable `parity.toml` and `parity_example.py`. Replace the generated
functions with import targets for your existing and rewritten transformations, then make the
equivalence policy match the real contract.

To scaffold an existing pair directly, supply the two targets and a fixture together. This mode
writes only `parity.toml`; it does not create demo code or install environments:

```bash
parity init --reference orders.pandas_impl:transform \
  --candidate orders.polars_impl:transform \
  --fixture tests/fixtures/orders.parquet \
  --reference-adapter pandas --candidate-adapter polars \
  --record-distribution orders-lib --row-key order_id
parity doctor --config parity.toml
parity check
```

A minimal case looks like this:

```toml
version = 1
artifact_dir = ".parity"

[[cases]]
name = "orders"
fixture = "tests/fixtures/orders.parquet"
tags = ["critical", "migration"]

[cases.reference]
target = "orders.pandas_impl:transform"
adapter = "pandas"
record_distributions = ["orders-lib"]

[cases.candidate]
target = "orders.polars_impl:transform"
adapter = "polars"
record_distributions = ["orders-lib"]

[cases.comparison]
row_order = "keyed"
row_keys = ["order_id"]
column_order = "strict"
dtype = "compatible"
rtol = 1e-7
atol = 0.0

[cases.generation]
max_examples = 250
max_findings = 3
stability_repeats = 2
search = true
seed = 20260813
shrink = true

[cases.performance]
enabled = true
max_slowdown = 1.25
max_memory_ratio = 1.5
fail_on_regression = false
```

Pandas callables receive Arrow-backed extension dtypes by default. If legacy code specifically
expects pandas' conventional NumPy/object dtypes, set `pandas_input = "native"` in that callable's
table. Native materialization can widen nullable integers and collapse null with IEEE NaN, so the
choice is saved in replay artifacts as part of the input contract.

Run one case, a tag, or the whole suite:

```bash
parity check
parity check --case orders --max-examples 1000
parity check --tag critical --json .parity/report.json --junit .parity/junit.xml
```

When Parity finds a mismatch it writes a directory like:

```text
.parity/orders/20260813T184205Z-a18d7e91/
├── input.arrow
├── input.parquet  # when the schema is representable in Parquet
├── manifest.json
├── replay.json
└── result.json
```

Multi-input failures use opaque `input-000.arrow`, `input-001.arrow`, … files bound to their
logical names in `replay.json`; the complete bundle is integrity-checked and replayed atomically.

Reproduce it without regenerating inputs:

```bash
parity replay .parity/orders/20260813T184205Z-a18d7e91
```

New artifacts bind replay to the Python and dependency versions observed inside both workers.
Parity probes both environments before importing either callable and returns an error if that
runtime contract drifted. Older artifacts remain replayable, but are visibly marked unverified
because they did not record an environment contract.

Exit code `0` means every selected case passed, `1` means a semantic or enforced performance
failure, and `2` means configuration or execution error.

## Gate a complete declared migration

Individual passing cases do not show that an agent migrated every intended API. Record the reviewed
surface in a separate manifest:

```toml
version = 1

[[units]]
id = "orders-transform"
cases = ["orders-control", "orders-null-keys"]

[[units]]
id = "plot-orders"
excluded_reason = "Presentation output is outside this migration."
```

The migration command is included in Parity 0.9.1. The `v0.9.0` tag did not publish because it
pointed at package version 0.8.1; do not use that tag for the migration gate.

Run the unfiltered coverage gate:

```bash
parity migration check \
  --manifest migrations/migration.toml \
  --config migrations/parity.toml \
  --json .parity/migration-status.json
```

The gate runs the union of mapped cases once. It passes only when at least one declared unit passed
and every other unit passed or was explicitly excluded. Failed or uncovered units return exit `1`;
invalid configuration or uncertain execution returns exit `2`. An all-excluded manifest fails, so
an empty scope cannot produce a vacuous success.

This establishes coverage only for the units declared in the manifest. It cannot discover an
omitted public API or prove that a mapped case genuinely exercises its claimed unit. Review the
inventory, wrappers and exclusions. See the [agent migration protocol](docs/AGENT_MIGRATION.md) for
the complete inventory, implementation, replay and dependency-matrix workflow.

## Generated inputs

A fixture anchors Parity in a real shape and schema. An explicit schema makes the explored domain
reviewable and reproducible:

```toml
[cases.schema]
min_rows = 0
max_rows = 50
unique_together = [["order_id"]]

[[cases.schema.constraints]]
kind = "sorted_by"
columns = ["order_id"]
descending = false
nulls = "last"

[[cases.schema.columns]]
name = "order_id"
dtype = "int64"
nullable = false
unique = true
minimum = 0

[[cases.schema.columns]]
name = "amount"
dtype = "float64"
nullable = true
minimum = -1000000.0
maximum = 1000000.0
examples = [0.0, -0.0, 0.1]
```

The generator targets empty and singleton frames, nulls, NaNs, infinities, signed zero, numeric
boundaries, duplicate values, awkward strings and temporal boundaries where the schema permits
them. A failing input is saved as data, not merely printed as a random seed; generated failures are
minimized when shrinking is enabled and succeeds. `sorted_by` and `row_comparison` constraints let
the search stay inside valid domains required by as-of joins, windows and interval calculations.

For outputs with stable identities, prefer keyed alignment to globally ignoring order:

```toml
[cases.comparison]
row_order = "keyed"
row_keys = ["customer_id", "period"]
```

Keys must be unique on both sides and match exactly; tolerances still apply to non-key payload
columns. Parity fails closed when keys are missing, duplicated, nested or ambiguous under the
selected name policy.

## Pytest

The installed plugin exposes a `parity` assertion fixture:

```python
def test_orders_migration(parity):
    parity.check("parity.toml", cases={"orders"})
```

Or verify live functions with the same API as `parity.verify`:

```python
from parity import ComparisonPolicy, FrameSchema, RowComparison, SortedBy


def test_live_rewrite(parity, orders_schema: FrameSchema):
    parity.verify(
        pandas_transform,
        polars_transform,
        schema=orders_schema,
        reference_adapter="pandas",
        candidate_adapter="polars",
        comparison=ComparisonPolicy(row_order="ignore"),
    )
```

Python callers can construct `FrameSchema(constraints=[SortedBy(...),
RowComparison(...)])`; configured TOML cases use the same validated models.

Use `--parity-config` and repeatable `--parity-case` options for CI selection, or an explicit
`@pytest.mark.parity(config="...", cases=[...])` override. See the
[pytest guide](docs/PYTEST.md).

## GitHub Actions

```yaml
permissions:
  contents: read

steps:
  - uses: actions/checkout@v4
  - uses: leighshepperson/parity@v0.8.0
    with:
      config: parity.toml
      cases: orders,customers
      upload-artifact: "true"
```

The action always adds a redacted Markdown report to the job summary. The example explicitly opts
into uploading `.parity` even on failure. Reports omit compared values, but replay artifacts contain
generated or fixture-derived input values; leave upload disabled unless your repository's access and
retention policy permits that data. See the [Action guide](docs/GITHUB_ACTION.md).

## Python API

```python
from parity import check, verify

suite = check("parity.toml", cases={"orders"})
assert suite.passed

suite = verify(
    pandas_transform,
    polars_transform,
    fixture=sample,
    reference_adapter="pandas",
    candidate_adapter="polars",
    artifact_dir=".parity/live-orders",
)
```

`check` and `verify` return typed Pydantic result models. They do not terminate the process; the
caller decides how to enforce the result.

The migration gate has the same non-terminating Python form:

```python
from parity import check_migration

migration = check_migration("migrations/migration.toml", "migrations/parity.toml")
assert migration.passed
```

Use `migration.status`, `migration.units` and `migration.suite` for structured automation. The CLI
adds the `0`/`1`/`2` process-exit contract and optional data-safe JSON report.

## Design boundaries

Parity answers: *did these two executable contracts differ anywhere we looked?* It does not decide
which implementation expresses business intent, prove correctness outside the input domain, make
a probabilistic test exhaustive, or justify weakening a comparison policy. Generated inputs are
synthetic probes; include representative, non-sensitive fixtures for application-specific
invariants.

The reference and candidate are untrusted project code from Parity's perspective. Process
isolation limits accidental interference but is not a security sandbox. Run unknown code in a
container or hardened CI runner. Full details are in [Security and privacy](docs/SECURITY.md) and
the [threat model](docs/THREAT_MODEL.md).

## Documentation

- [Release notes](CHANGELOG.md)
- [External validation log](case_studies/ADOPTION_LOG.md)
- [Getting started and migration workflow](docs/USER_GUIDE.md)
- [Agent migration protocol and completion gate](docs/AGENT_MIGRATION.md)
- [Configuration reference](docs/CONFIG_REFERENCE.md)
- [Architecture and artifact contracts](docs/ARCHITECTURE.md)
- [Fault corpus](docs/FAULT_CORPUS.md)
- [Real-world case study: pyjanitor `complete()`](case_studies/pyjanitor_complete/README.md)
- [Version-matrix case study: skrub `AggJoiner`](case_studies/skrub_agg_joiner/README.md)
- [Multi-input case study: pandas `merge` / Polars `join`](case_studies/pandas_polars_join/README.md)
- [Valid-domain case study: pandas `merge_asof` / Polars `join_asof`](case_studies/pandas_polars_asof/README.md)
- [Same-input stability probe](case_studies/stability_probe/README.md)
- [Keyed-output control: utilsforecast `evaluate`](case_studies/utilsforecast_evaluate/README.md)
- [Public-project backend study: PyIndicators `ema`](case_studies/pyindicators_ema/README.md)
- [Five-API migration pilot: PyTimeTK pandas to Polars](case_studies/pytimetk_migration/README.md)
- [Cross-version study: Polars `group_by_dynamic`](case_studies/polars_version_dynamic/README.md)
- [Cross-version study: pandas categorical `groupby`](case_studies/pandas_version_groupby/README.md)
- [Development and contribution guide](docs/DEVELOPMENT.md)
- [Technical roadmap](docs/ROADMAP.md)
- [Clean-room provenance](docs/CLEAN_ROOM.md) and [public prior art](docs/PRIOR_ART.md)

## Development status

Parity `0.8` is an alpha: useful on real migrations, but its configuration and artifact contracts
may evolve before `1.0`. Issues and small, synthetic reproduction cases are welcome.

Licensed under the [Apache License 2.0](LICENSE).
