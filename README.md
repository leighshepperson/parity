# Parity

**Behavioural compatibility testing for software migrations.**

Parity runs a reference and a candidate on the same inputs, compares what they return or raise,
searches for differences, shrinks failing inputs and saves replayable evidence.

```text
canonical input ──┬──> reference ──┐
                  └──> candidate ──┴──> compare ──> shrink ──> replay
```

The two sides can use different dependency versions, APIs, implementations, Python environments or
languages. They only need a small shared behavioural contract.

Use Parity for dependency upgrades, refactors, backend changes, branch/worktree comparisons and
cross-language rewrites. It verifies a migration; it does not write or repair one.

> `PASSED` means Parity found no difference in the configured domain and search budget. It is
> executable evidence, not a proof of equivalence.

## Five-minute start

The Parity controller requires Python 3.11 or later.

```bash
python -m pip install parity-check
parity init
parity check
```

`parity init` creates a runnable `parity.toml` and `parity_example.py`. Edit the two example
functions to call the old and new behaviour, then rerun `parity check`.

The base package uses Arrow and does not install pandas or Polars. Add `parity-check[pandas]` or
`parity-check[polars]` when targets in the controller environment use those adapters. Managed
package-upgrade environments use `parity-check[workspace]`.

## Put it around real code

A useful first case needs a representative input and an explicit comparison policy:

```toml
version = 1

[[cases]]
name = "orders"
fixture = "tests/fixtures/orders.parquet"

[cases.reference]
target = "migration_adapters:reference_orders"
adapter = "pandas"

[cases.candidate]
target = "migration_adapters:candidate_orders"
adapter = "polars"

[cases.comparison]
row_order = "keyed"
row_keys = ["order_id"]
dtype = "compatible"
rtol = 1e-7

[cases.generation]
max_examples = 250
max_findings = 4

[cases.performance]
enabled = false
```

Paths are relative to `parity.toml`. Targets use `module:callable` syntax and should be small,
project-owned wrappers around the public behaviour being migrated.

```bash
parity doctor --config parity.toml
parity check --config parity.toml
parity check --case orders --max-examples 1000
parity check --json .parity/report.json --junit .parity/junit.xml
```

The CLI has a stable outcome contract:

- exit `0`: `PASSED`;
- exit `1`: `FAILED` because behaviour or an enforced performance policy differed; and
- exit `2`: `ERROR` because configuration or execution could not produce reliable evidence.

## What Parity checks

- Arrow, pandas and Polars inputs, including two- and three-frame joins or lookups.
- Returned frames and JSON-like values, raised exceptions and input mutation.
- Column and row order, keyed rows, dtypes, null/NaN rules, numeric tolerances and datetimes.
- Fixtures, deterministic edge cases, Hypothesis generation and shrinking.
- Several independent mismatch classes in one run, each with a stable `ms3:` identifier.
- Runtime and selected dependency provenance for each isolated target.
- Optional paired runtime and peak-memory regression evidence after semantic success.

Reference and candidate signatures do not need to match. Adapt each side into the shared input and
output contract:

```python
def reference_quote(frame):
    from legacy import calculate

    row = frame.iloc[0]
    return calculate(row.x, row.y, row.currency)


def candidate_quote(frame):
    from rewritten import Data, Engine

    row = frame.iloc[0]
    return Engine(row.currency).calculate(Data(row.x, row.y))
```

Keep side-specific imports inside their wrappers when the two environments intentionally contain
different packages. A configured `canonicalizer` can project a successful domain object into an
Arrow or JSON-like result. Target exceptions remain observable behaviour.

## Upgrade two released packages

The managed workspace creates separate, locked target environments. The controller and targets do
not share a dependency graph.

```bash
python -m pip install "parity-check[workspace]"
parity migration init \
  --reference-package 'your-library==1.2.3' \
  --candidate-package 'your-library==2.0.0' \
  --scaffold \
  --json
```

This creates a deliberately incomplete adapter, tiny JSON fixture, case configuration, migration
inventory, workspace and four-item review checklist under `migrations/`. Implement the adapter,
review the fixture/domain and comparison policy, resolve the checklist, then run:

```bash
parity migration validate --json
parity migration run --json
```

Validation does not create environments or invoke targets. It remains non-passing until the
generated contract has been reviewed. The final run prepares both sides, checks every declared
case in every dependency lane and writes a data-safe report per lane.

Each side may instead be an existing checkout, so the same workflow covers released/local and
local/local comparisons:

```bash
parity migration init \
  --reference-path ../main-worktree \
  --candidate-path ../feature-worktree
parity migration run
```

Parity does not clone, switch or edit worktrees. It installs each checkout separately and binds its
Git/content identity into the evidence. See the [user guide](docs/USER_GUIDE.md) for custom targets,
dependency lanes and rolling A→B→C upgrades.

## Cross-language targets

Any local executable can be a reference or candidate through Parity's versioned Arrow/JSON target
protocol. For a Python boundary around C, C++, Fortran, Rust, Java or a legacy CLI, scaffold the
protocol process and implement only the domain translation:

```bash
parity adapter init adapters/legacy.py --program bin/legacy-target
```

```toml
[cases.reference]
command = ["parity", "adapter", "serve", "adapters/legacy.py"]
```

The adapter process needs Parity; the wrapped program does not. Compilation, images and dependency
installation stay outside the behavioural contract. Start with the
[adapter SDK guide](docs/TARGET_ADAPTER_SDK.md); implement the
[language-neutral protocol](docs/TARGET_PROTOCOL.md) directly only when Python is unsuitable.

## Findings and replay

A confirmed difference creates a private artifact containing the minimized Arrow input, hashes,
comparison contract, runtime identities and exact replay information:

```bash
parity replay .parity/orders/<finding-directory>
parity evidence verify .parity/report.json --json .parity/evidence-status.json
```

`replay` reproduces the semantic result, so a reproduced incompatibility still exits `1`.
`evidence verify` answers a different question and exits `0` when every report-referenced finding
reproduces its recorded mismatch class.

Terminal, JSON, Markdown and JUnit reports omit compared values. Counterexample and distilled
contract directories contain real inputs and outputs; keep them private and upload them only when
your data policy permits it.

Reviewed intentional differences can be recorded as exact case/finding approvals. Discovered
regressions can also be distilled into a candidate-only contract before the old implementation is
removed. See [compatibility budgets](docs/COMPATIBILITY_BUDGETS.md) and
[distilled contracts](docs/DISTILLED_CONTRACTS.md).

## CI

```yaml
permissions:
  contents: read

steps:
  - uses: actions/checkout@v4
  - uses: leighshepperson/parity@v0
    with:
      config: parity.toml
      performance: "false"
```

The moving `v0` Action installs the same Parity source revision as the selected action. Pin a
reviewed full commit SHA when CI must be immutable. Artifact upload is opt-in because findings can
contain sensitive inputs. See the [GitHub Action guide](docs/GITHUB_ACTION.md).

## Read next

| Need | Document |
|---|---|
| Build a practical campaign | [User guide](docs/USER_GUIDE.md) |
| Look up every TOML field | [Configuration reference](docs/CONFIG_REFERENCE.md) |
| Decide whether Parity fits | [Use cases and boundaries](docs/USE_CASES.md) |
| Verify a coding-agent migration | [Agent migration protocol](docs/AGENT_MIGRATION.md) |
| Use pytest | [Pytest integration](docs/PYTEST.md) |
| Understand internals and contracts | [Architecture](docs/ARCHITECTURE.md) |
| Handle artifacts and untrusted code | [Security and privacy](docs/SECURITY.md) |
| Explore executable examples | [Fault corpus](examples/pandas_polars/README.md) and [case studies](case_studies/ADOPTION_LOG.md) |

## Boundaries and status

Parity compares canonical returns, raises, mutation and process performance. A reviewed wrapper can
project a CLI, file or database result into that contract, but Parity does not yet capture and
restore filesystem, database or network effects itself. Target processes isolate failures; they are
not a security sandbox for hostile code.

Parity is Apache-2.0 licensed and pre-1.0. The current minor release is the supported line, and a
minor release may deliberately change public contracts before 1.0.
