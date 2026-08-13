# Parity

**Differential semantic testing for changed computation.**

Parity tries to disprove that a rewrite means the same thing as the implementation it replaces.
It executes both versions over fixtures and generated edge cases, compares their observable
behaviour under an explicit policy, shrinks a failure to a small counterexample and preserves it
for replay. Performance is measured only after correctness.

The initial adapters support pandas-to-Polars comparisons. The contracts and artifact format are
engine-neutral so the same approach can cover other dataframe, numerical and dependency changes.

> Parity is an evidence generator, not a proof of mathematical equivalence. Passing means no
> difference was found within the configured domain and search budget.

## What one run gives you

- Deterministic fixture checks plus property-based exploration and shrinking.
- Cross-engine comparison of shape, columns, rows, dtypes, nulls, NaNs, signed zero, numeric
  tolerances, datetimes, returned exceptions and input mutation.
- Isolated execution with per-case timeouts and separate Python environments when configured.
- Reproducible Arrow counterexamples, plus Parquet when representable, with a manifest and replay
  command.
- Median runtime and peak-memory comparisons with optional regression gates.
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

[cases.candidate]
target = "orders.polars_impl:transform"
adapter = "polars"

[cases.comparison]
row_order = "ignore"
column_order = "strict"
dtype = "compatible"
rtol = 1e-7
atol = 0.0

[cases.generation]
max_examples = 250
seed = 20260813
shrink = true

[cases.performance]
enabled = true
max_slowdown = 1.25
max_memory_ratio = 1.5
fail_on_regression = false
```

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

Reproduce it without regenerating inputs:

```bash
parity replay .parity/orders/20260813T184205Z-a18d7e91
```

Exit code `0` means every selected case passed, `1` means a semantic or enforced performance
failure, and `2` means configuration or execution error.

## Generated inputs

A fixture anchors Parity in a real shape and schema. An explicit schema makes the explored domain
reviewable and reproducible:

```toml
[cases.schema]
min_rows = 0
max_rows = 50
unique_together = [["order_id"]]

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
them. A minimized failure is always saved as data, not merely printed as a random seed.

## Pytest

The installed plugin exposes a `parity` assertion fixture:

```python
def test_orders_migration(parity):
    parity.check("parity.toml", cases={"orders"})
```

Or verify live functions with the same API as `parity.verify`:

```python
from parity import ComparisonPolicy, FrameSchema


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

Use `--parity-config` and repeatable `--parity-case` options for CI selection, or an explicit
`@pytest.mark.parity(config="...", cases=[...])` override. See the
[pytest guide](docs/PYTEST.md).

## GitHub Actions

```yaml
permissions:
  contents: read

steps:
  - uses: actions/checkout@v4
  - uses: leighshepperson/parity@v0.1.0
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

`check` and `verify` return stable Pydantic result models. They do not terminate the process; the
caller decides how to enforce the result.

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

- [Getting started and migration workflow](docs/USER_GUIDE.md)
- [Configuration reference](docs/CONFIG_REFERENCE.md)
- [Architecture and artifact contracts](docs/ARCHITECTURE.md)
- [Fault corpus](docs/FAULT_CORPUS.md)
- [Real-world case study: pyjanitor `complete()`](case_studies/pyjanitor_complete/README.md)
- [Development and contribution guide](docs/DEVELOPMENT.md)
- [Technical roadmap](docs/ROADMAP.md)
- [Clean-room provenance](docs/CLEAN_ROOM.md) and [public prior art](docs/PRIOR_ART.md)

## Development status

Parity `0.1` is an alpha: useful on real migrations, but its configuration and artifact contracts
may evolve before `1.0`. Issues and small, synthetic reproduction cases are welcome.

Licensed under the [Apache License 2.0](LICENSE).
