# Parity

**Migration verification by observable behaviour—across versions, implementations, runtimes and
languages.**

Parity runs a reference and a candidate on the same complete calls, compares what they return or
raise, searches for differences, shrinks failing invocations and saves replayable evidence.

```text
canonical call ──┬──> reference ──┐
                 └──> candidate ──┴──> compare ──> shrink ──> replay
```

Parity's unit of work is an explicit `callable(*args, **kwargs)` contract. A complete call can
combine ordinary JSON, frames, variable-length sequences or project-generated structures such as
recursive programs and event streams. The two sides can use different dependency versions, APIs,
architectures, Python environments or languages; they only need the same observable contract.

Use Parity to verify dependency upgrades, refactors, backend changes, branch/worktree comparisons
and cross-language rewrites. Migration authoring and repair remain in the surrounding development
workflow.

> `PASSED` means Parity found no difference in the configured domain and search budget. It is
> executable evidence, not a proof of equivalence.

## Five-minute start

The Parity controller requires Python 3.11 or later.

```bash
python -m pip install parity-check
parity init
parity check
```

`parity init` creates a runnable, JSON-only `parity.toml` and `parity_example.py`. Edit the two
example functions to call the old and new behaviour, then rerun `parity check`.

The standard installation can also create isolated reference and candidate environments for
dependency upgrades and worktree comparisons. Install `parity-check[pandas]` or
`parity-check[polars]` only when targets in the controller environment use those adapters; the
base package already handles JSON and Arrow calls.

## Put it around real code

A useful first case needs representative calls and an explicit comparison policy. This example
compares two pricing-rules implementations using only JSON values:

```toml
version = 2

[[cases]]
name = "pricing-rules"

[[cases.invocation.args]]
kind = "json"
values = [
  { plan = "basic", seats = 1, coupons = [] },
  { plan = "pro", seats = 25, coupons = ["LOYALTY"] },
]

[cases.invocation.kwargs.region]
kind = "json"
values = ["GB", "US"]

[cases.reference]
target = "migration_adapters:reference_quote"

[cases.candidate]
target = "migration_adapters:candidate_quote"

[cases.comparison]
check_exceptions = true
check_input_mutation = true
rtol = 0.0
atol = 0.0

[cases.generation]
max_examples = 250
max_findings = 4

[cases.performance]
enabled = false
```

Paths are relative to `parity.toml`. Targets use `module:callable` syntax and should be small,
project-owned wrappers around the public behaviour being migrated.

Repeat `[[cases]]` for independent behaviours. Inside one case, repeat
`[[cases.invocation.args]]` or add `[cases.invocation.kwargs.<name>]` for many inputs. Use
`kind = "frame"` when table structure is part of the contract, `kind = "frames"` for one
variable-length frame sequence, or a project-owned generator for dependent structures such as
ASTs and stateful event streams. Zero-argument calls, expanded `*frames` and relationally generated
joins are supported too. See [invocation configuration](docs/CONFIG_REFERENCE.md#invocation).

```bash
parity doctor --config parity.toml
parity check --config parity.toml
parity check --case pricing-rules --max-examples 1000
parity check --json .parity/report.json --junit .parity/junit.xml
```

The CLI has a stable outcome contract:

- exit `0`: `PASSED`;
- exit `1`: `FAILED` because behaviour or an enforced performance policy differed; and
- exit `2`: `ERROR` because configuration or execution could not produce reliable evidence.

## What Parity checks

- Complete calls with zero or many positional and keyword JSON values, frames and frame sequences.
- Nested JSON or frame returns, raised exceptions and input mutation.
- Fixtures, deterministic edge cases, built-in generation and project-owned Hypothesis strategies
  for arbitrary bounded domains such as recursive ASTs or operation streams.
- When frames are present: column and row order, keyed rows, dtypes, null/NaN rules, numeric
  tolerances, datetimes and relational constraints.
- Several independent mismatch classes in one run, each with a stable `ms3:` identifier.
- Runtime and selected dependency provenance for each isolated target.
- Optional paired runtime and peak-memory regression evidence after semantic success.

Both sides receive the exact same call shape and values. Their internal APIs do not need to match;
adapt each side into the shared input and output contract:

```python
def reference_quote(request, *, region):
    from legacy import quote

    return quote(request, market=region)


def candidate_quote(request, *, region):
    from rewritten import Engine, QuoteRequest

    result = Engine(region=region).quote(QuoteRequest.from_dict(request))
    return {"decision": result.status, "price": result.amount, "reasons": result.reasons}
```

Keep side-specific imports inside their wrappers when the two environments intentionally contain
different packages. A configured `canonicalizer` can project a successful domain object into an
Arrow or JSON-like result. Target exceptions remain observable behaviour.

## Upgrade two released packages

Parity creates a separate, locked environment for each side, so the controller, reference and
candidate do not share a dependency graph. Their source and environment declaration is stored in
`migrations/parity.workspace.toml`; generated environments and locks remain private state.

```bash
parity migration init \
  --reference-package 'your-library==1.2.3' \
  --candidate-package 'your-library==2.0.0' \
  --scaffold \
  --json
```

This creates a deliberately incomplete adapter, tiny JSON fixture, case configuration, migration
inventory, environment declaration and four-item review checklist under `migrations/`. Implement
the adapter, review the fixture/domain and comparison policy, resolve the checklist, then run:

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

The maintained [JavaScript-to-Python rules-engine proof](case_studies/javascript_python_rules/README.md)
uses recursive JSON programs, nested returns and domain exceptions. It verifies a correct port,
discovers and minimizes three independent defects in a naive port, retains them as regressions and
replays the saved evidence.

## Findings and replay

A confirmed difference creates a private artifact containing the minimized invocation, any Arrow
leaves, hashes, comparison contract, runtime identities and exact replay information:

```bash
parity replay .parity/pricing-rules/<finding-directory>
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
| See a JSON-only cross-language proof | [JavaScript to Python rules engine](case_studies/javascript_python_rules/README.md) |
| Build a practical campaign | [User guide](docs/USER_GUIDE.md) |
| Look up every TOML field | [Configuration reference](docs/CONFIG_REFERENCE.md) |
| Decide whether Parity fits | [Use cases and boundaries](docs/USE_CASES.md) |
| Verify a coding-agent migration | [Agent migration protocol](docs/AGENT_MIGRATION.md) |
| Use pytest | [Pytest integration](docs/PYTEST.md) |
| Understand internals and contracts | [Architecture](docs/ARCHITECTURE.md) |
| Handle artifacts and untrusted code | [Security and privacy](docs/SECURITY.md) |
| Explore dataframe migrations | [pandas-to-Polars fault corpus](examples/pandas_polars/README.md) |
| Explore other executable proofs | [C++ order book](case_studies/cpp_python_orderbook/README.md), [Fortran summation](case_studies/fortran_python/README.md) and [external validation](case_studies/ADOPTION_LOG.md) |

## Boundaries and status

Parity compares canonical returns, raises, mutation and process performance. A reviewed wrapper can
project a CLI, file or database result into that contract, but Parity does not yet capture and
restore filesystem, database or network effects itself. Target processes isolate failures; they are
not a security sandbox for hostile code.

Parity is Apache-2.0 licensed and pre-1.0. The current minor release is the supported line, and a
minor release may deliberately change public contracts before 1.0.
