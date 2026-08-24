# User guide

This guide takes a real migration from one representative example to a repeatable release gate.
For every TOML field and validation rule, use the
[configuration reference](CONFIG_REFERENCE.md).

The running example follows an orders backend change, making ordering, dtype and null policies
concrete. The same workflow applies to complete JSON calls: `parity init` creates a positional and
keyword JSON starter, and the maintained
[JavaScript-to-Python rules-engine study](../case_studies/javascript_python_rules/README.md) is a
fully executable recursive-JSON campaign.

## 1. Choose the behavioural contract

Parity compares observations, not source code. Start with a boundary a user or downstream system
cares about: a public transformation, model operation, parser, calculation, stateful lifecycle or
serialized result.

A good contract states:

- which inputs are valid;
- how each implementation receives them;
- which returned values or raised exceptions are equivalent;
- whether row/column order, dtypes, nulls and input mutation matter; and
- which dependency/runtime versions are part of the claim.

Keep target wrappers small. They should translate the canonical input into each API and return a
shared Arrow/frame or JSON-like observation:

```python
def reference_total(frame):
    from old_orders import calculate_total

    return calculate_total(frame, currency="GBP")


def candidate_total(frame):
    from new_orders import Engine

    return Engine(currency="GBP").total(frame)
```

Imports inside side-specific functions are useful when the reference and candidate environments
contain conflicting packages. If a successful call returns a domain object, configure a small
`canonicalizer` in that target environment. Do not catch domain exceptions merely to turn them into
returns: Parity compares raised exceptions as first-class behaviour.

Parity is strongest when the boundary is deterministic and mostly input-to-output. A wrapper may
project a CLI, file or database effect into a returned summary, but the wrapper then owns isolation,
cleanup and the fidelity of that projection.

## 2. Create the first case

Install the controller and any adapters it uses:

```bash
python -m pip install parity-check
# Add parity-check[pandas], parity-check[polars], or both when needed.
```

Generate an editable starter:

```bash
parity init
```

For existing wrappers and a fixture, Parity can write a compact project configuration directly:

```bash
parity init migrations/parity.toml \
  --reference migration_adapters:reference_total \
  --candidate migration_adapters:candidate_total \
  --fixture tests/fixtures/orders.parquet \
  --case-name orders \
  --reference-adapter pandas \
  --candidate-adapter polars \
  --row-key order_id
```

The reference, candidate and fixture options are all-or-none. The command validates their syntax
and refuses to replace an existing file unless `--force` is explicit.

A case usually starts with a small, non-sensitive fixture and a comparison policy:

```toml
version = 2
artifact_dir = ".parity"

[[cases]]
name = "orders"
tags = ["critical"]

[[cases.invocation.args]]
kind = "frame"
fixture = "tests/fixtures/orders.parquet"

[cases.reference]
target = "migration_adapters:reference_total"
adapter = "pandas"
record_distributions = ["orders-lib"]

[cases.candidate]
target = "migration_adapters:candidate_total"
adapter = "polars"
record_distributions = ["orders-lib"]

[cases.comparison]
row_order = "keyed"
row_keys = ["order_id"]
column_order = "strict"
dtype = "compatible"
null_equal = true
nan_equal = true
rtol = 1e-7
atol = 0.0

[cases.generation]
max_examples = 250
max_findings = 4
stability_repeats = 2

[cases.performance]
enabled = false
```

Paths resolve beside the root `parity.toml`, not from the shell's current directory. Both sides
receive this exact positional frame; use wrappers if their internal signatures differ.

Choose policies from consumer behaviour, not from whatever makes the first run green. In
particular, decide whether output order is positional, irrelevant, or keyed by a unique business
identity. Use tolerances for measured numerical error; do not use them to hide structural changes.

## 3. Preflight, run and interpret

```bash
parity doctor --config migrations/parity.toml
parity check --config migrations/parity.toml
parity check --config migrations/parity.toml --case orders --no-performance
parity check --config migrations/parity.toml --tag critical \
  --json migrations/.parity/report.json
```

`doctor --config` checks both transports and runtime requirements before it imports either target,
then checks target/adaptor/canonicalizer imports without invoking the behaviour under test.

The three result states are deliberately different:

- `PASSED` / exit `0`: every observation satisfied the selected policy;
- `FAILED` / exit `1`: a semantic difference or enforced performance regression was found; and
- `ERROR` / exit `2`: configuration, setup or execution could not produce trustworthy evidence.

A matching exception is a matching observation, not necessarily a successful business result.
Timeouts, crashes, import failures, invalid protocol responses and nondeterminism are errors rather
than semantic findings.

For each configured case, Parity keeps one reference and one candidate process alive for the
campaign. Every call gets a fresh deserialized input, but module globals and other process state
persist. `stability_repeats` rechecks passing deterministic inputs independently on each side so
matching hidden state cannot create a false pass.

## 4. Review and replay findings

A confirmed finding is saved under the configured artifact root. The directory contains a
minimized invocation, hash-bound Arrow leaves, the private reference observation and the exact
replay contract. Reports point to the artifact but omit compared values.

```bash
parity replay migrations/.parity/orders/<finding-directory>
parity evidence verify migrations/.parity/report.json \
  --json migrations/.parity/evidence-status.json
```

`replay` returns the semantic outcome: reproducing a mismatch exits `1`. `evidence verify` checks
all findings referenced by a suite or migration report and exits `0` when they reproduce the same
`ms3:` mismatch classes.

Treat `ms3:` as a stable, data-free classifier for deduplication and replay. It is not a digital
signature, root-cause diagnosis or approval. Inspect the private artifact before deciding whether a
difference is a candidate bug, reference bug, invalid domain or intentional contract change.

Replay resolves project-relative paths from the artifact's recorded relationship to the directory
containing `parity.toml`; it does not depend on the current working directory. External or missing
interpreters, workdirs and executables leave an explicit replay blocker rather than silently using a
different local program.

## 5. Keep target environments independent

Set a separate `python` executable on each endpoint when environments are prepared outside Parity:

```toml
[cases.reference]
target = "migration_adapters:reference_total"
python = ".venv-reference/bin/python"
adapter = "pandas"
required_distributions = { orders-lib = "==1.*" }

[cases.candidate]
target = "migration_adapters:candidate_total"
python = ".venv-candidate/bin/python"
adapter = "pandas"
required_distributions = { orders-lib = "==2.*" }
```

Each Python target environment needs PyArrow, its selected adapter dependency and the application.
It does not need the Parity controller, Pydantic, Hypothesis, Rich or Typer. The controller runs a
dependency-light portable worker in that interpreter. `record_distributions` records relevant
versions; `required_distributions` rejects an unexpected or missing version before target import.

For a non-Python target, configure a protocol-speaking command instead of `target`:

```toml
[cases.reference]
command = ["./bin/legacy-adapter"]

[cases.candidate]
command = ["./bin/new-adapter", "--compat"]
```

Commands are argument vectors, not shell strings. They own input adaptation and output
canonicalization through [target protocol v2](TARGET_PROTOCOL.md). If Python is a suitable boundary
around the external program, `parity adapter init` generates a small SDK adapter so project code
only implements target inspection and translation. See the
[command-adapter SDK guide](TARGET_ADAPTER_SDK.md).

## 6. Let Parity manage both environments

For a dependency upgrade or worktree comparison, Parity can prepare and lock an isolated
environment for each side. Declare the two reviewed sources:

```bash
parity migration init \
  --reference-package 'orders-lib==1.4.2' \
  --candidate-package 'orders-lib==2.0.0' \
  --scaffold \
  --json
```

`--scaffold` atomically creates, under `migrations/`:

- `migration_adapters.py`, containing an intentionally incomplete Arrow wrapper;
- `fixtures/input.json`, a tiny placeholder input;
- `parity.toml`, with performance disabled until the semantic contract is reviewed;
- `migration.toml`, whose starter `core-regression` unit maps the configured cases;
- `migration.checklist.json`, with four explicit review decisions; and
- `parity.workspace.toml`, the v3 source and environment declaration.

The command never overwrites an authored scaffold file. Implement the adapter, replace or review
the fixture, confirm the comparison/domain and inventory, then set every checklist item's `status`
and the top-level `status` to `resolved`. Mark them resolved only after the corresponding review;
the checklist is an explicit decision record, not an automatic discovery result.

```bash
parity migration validate --json
parity migration run --json
```

Validation loads the workspace, cases, inventory, checklist and fixtures without creating target
environments or invoking targets. It exits `1` while review remains and `0` when the authored
contract is structurally ready. `migration run` then resolves reusable hash-pinned locks, prepares
isolated workers, enforces the exact package versions and executes the complete inventory in every
lane.

If the wrappers and fixture already exist, create the first contract without generated placeholders:

```bash
parity migration init \
  --reference-package 'orders-lib==1.4.2' \
  --candidate-package 'orders-lib==2.0.0' \
  --target migration_adapters:orders_contract \
  --fixture tests/fixtures/orders.parquet
```

Use `--reference-target` and `--candidate-target` when the two APIs need different wrappers. If
`migrations/parity.toml` already exists, omit all case-scaffolding flags: Parity loads the reviewed
contract and creates only missing workspace/inventory files. `--force` replaces the workspace
declaration, never `parity.toml`.

### Source combinations and dependency lanes

Each side may be an exact released requirement or an existing local checkout:

```bash
parity migration init \
  --reference-path ../main-worktree \
  --candidate-path ../feature-worktree \
  --lane minimum=requirements/minimum.txt \
  --lane current=requirements/current.txt
parity migration run
```

Released/released, released/local, local/released and local/local use the same managed-environment
workflow. The reference always needs one package/path source. Omitting both candidate flags means
`--candidate-path .`.

For a coordinated dependency upgrade, put the before and after dependency sets in the project
metadata of their respective checkouts. Parity resolves and hash-locks each checkout independently,
so any number of direct and transitive packages may change together. A lane requirements file is a
shared constraint applied to both sides; use lanes for compatibility floors or current dependency
sets, not as side-specific before/after manifests.

Parity never creates, switches or edits worktrees. It installs a local source only into its own
worker, verifies the import origin and binds a path-free Git/content identity into findings and
replay. Local sources must be Git worktrees with committed `HEAD`; a dirty tree is allowed but its
content is part of the identity. A local/local run also checks both sources throughout execution and
writes paired `source-provenance.json` evidence.

Use `--reference-python` and `--candidate-python` for different Python 3.8+ target runtimes. The
Parity controller itself remains on Python 3.11+. Managed packages or lane requirements must supply
pandas, Polars or other optional target dependencies when their adapters need them.

Locks, environments, setup logs and the default package cache live under the private
`.parity/workspace/` state. Reuse locks for repeatability; pass `--refresh-locks` only when changing
dependency selection deliberately. Users do not author tox configuration or invoke the underlying
environment tools directly.

### Rolling A→B→C upgrades

Keep reusable controls and only the active adjacent pair. After B is accepted, move the baseline:

```bash
parity migration advance --reference-package 'orders-lib==2.0.0'
```

`advance` changes only an exact released reference, preserves lanes and invalidates current lane
reports. Update `candidate_package` separately to the reviewed C release before the next run.
Parity intentionally does not turn the active completion gate into a migration-history database.

## 7. Declare the migration inventory

`migration.toml` makes uncovered work visible:

```toml
version = 1

[[units]]
id = "orders-transform"
cases = ["orders-control", "orders-null-keys"]

[[units]]
id = "customer-summary"

[[units]]
id = "plot-orders"
excluded_reason = "Presentation output is outside this migration."
```

`customer-summary` is deliberately uncovered and keeps the gate red. An excluded unit needs a
specific reviewed reason. Run the inventory against externally managed target environments with:

```bash
parity migration check \
  --manifest migrations/migration.toml \
  --config migrations/parity.toml \
  --json migrations/.parity/migration-status.json
```

The gate runs the union of mapped cases once and attempts all of them. It has no case, tag, search or
performance overrides because a partial run cannot certify the inventory. It proves only that all
**declared** in-scope units passed; it cannot discover an omitted public API or prove that a named
case genuinely covers its unit.

## 8. Expand the input domain

A fixture anchors real structure. Add a reviewed schema when its inferred values or bounds are too
narrow:

```toml
[cases.invocation.args.schema]
min_rows = 0
max_rows = 50

[[cases.invocation.args.schema.columns]]
name = "status"
dtype = "string"
nullable = false
categories = ["open", "closed"]

[[cases.invocation.args.schema.columns]]
name = "start_date"
dtype = "date"
nullable = false

[[cases.invocation.args.schema.columns]]
name = "end_date"
dtype = "date"
nullable = false

[[cases.invocation.args.schema.constraints]]
kind = "row_comparison"
left = "start_date"
operator = "le"
right = "end_date"
```

Schemas support bounds, nullability, enums, examples, text constraints, time zones, uniqueness,
frame ordering and per-row comparisons. Deterministic boundary inputs run before generated search.

An invocation can contain as many fixed positional arguments as the callable needs. Repeat
`[[cases.invocation.args]]`, use `[cases.invocation.kwargs.<name>]` for keyword frames or JSON
modes, and add foreign-key, overlap, row-count or cardinality relationships between named frames.
Parity generates the complete call jointly and sends exactly the same call to both sides.

For one list-valued frame argument, use `kind = "frames"`. For a reduce that accepts `*frames`, use
a variable-length sequence expanded as varargs:

```toml
[cases.invocation.varargs]
kind = "frames"
name = "parts"
min_items = 1
max_items = 32
container = "tuple"

[cases.invocation.varargs.schema]
min_rows = 0
max_rows = 100

[[cases.invocation.varargs.schema.columns]]
name = "value"
dtype = "float64"
```

An empty `[cases.invocation]` table tests a zero-argument callable. The built-in contract supports
up to 256 positional/keyword call slots and 256 items per frame sequence; these are protocol safety
bounds, not the old one-to-three-frame model.

For a heterogeneous or dependent call shape that does not fit the compact argument strategies,
configure a project-owned generator factory instead of `[cases.invocation]`:

```toml
[cases.generation]
generator = "tests.generators:portfolios"
max_examples = 500
```

The factory returns a Hypothesis strategy of `parity.Invocation` objects, retaining shrinking, or a
bounded iterable of them from an existing deterministic corpus. Each object contains the complete
`args` and `kwargs`. Generator code is trusted project code and runs in the controller.

If a returned mapping contains several tables with different semantics, keep one case and patch the
comparison at an output JSON Pointer instead of weakening every result:

```toml
[[cases.comparison.overrides]]
path = "/joined"
row_order = "keyed"
row_keys = ["id"]
```

`*` matches one path segment; JSON Pointer escapes are `~0` for `~` and `~1` for `/`. A selected
policy is inherited by that subtree, and later matching overrides refine earlier broad ones.

For an exact fixture-only contract, set both `search = false` and
`adversarial_examples = false`. A searchless case without a deterministic input is rejected rather
than reported as a pass.

## 9. Handle intentional changes and retire the reference

Do not weaken the global policy for one reviewed difference. Capture exact case/finding approvals:

```bash
parity budget init .parity/report.json compatibility.toml
parity budget approve compatibility.toml orders ms3:... \
  --reason "The new API intentionally returns an empty result instead of raising."
```

Add `compatibility_budget = "compatibility.toml"` to `parity.toml` and rerun the full gate. Approved
findings remain visible; review/rejected entries and any new signature still fail. See
[compatibility budgets](COMPATIBILITY_BUDGETS.md).

To preserve discovered regressions without retaining or executing the old implementation:

```bash
parity contract distill .parity/report.json .parity/contracts/upgrade
parity contract verify .parity/contracts/upgrade
```

If intentional approved differences remain, promote stable candidate observations into a final
candidate-only baseline:

```bash
parity contract retire \
  .parity/contracts/upgrade \
  .parity/contracts/retired \
  --budget compatibility.toml
parity contract verify .parity/contracts/retired
```

A distilled contract contains only the distinct examples Parity found. It is a focused regression
corpus, not a recording of every passing input or every public behaviour. See
[distilled contracts](DISTILLED_CONTRACTS.md).

## 10. Add CI and performance deliberately

Use the [composite GitHub Action](GITHUB_ACTION.md), the CLI, or the
[pytest fixture](PYTEST.md) in CI. Keep the unfiltered migration gate as the final acceptance step;
focused cases are for iteration.

Run independent cases concurrently with `jobs` or `--jobs`. Use `native_threads` or
`--native-threads` to prevent BLAS/OpenMP oversubscription. Search and shrinking inside one case
remain serial and results retain configuration order.

Performance starts only after semantic success. Parity alternates paired reference/candidate runs
and reports median runtime and peak-memory ratios with deterministic bootstrap confidence
intervals. For an enforced or retained performance claim, use `jobs = 1`, enough repeats, a
controlled runner and a threshold justified by the application. It is regression evidence, not a
general microbenchmark suite.

## Command map

Use `parity --help` and `parity COMMAND --help` for the exact current options.

| Goal | Command |
|---|---|
| Create a starter or project case | `parity init` |
| Infer a portable schema | `parity inspect FIXTURE` |
| Preflight target environments | `parity doctor --config PATH` |
| Run configured cases | `parity check --config PATH` |
| Reproduce one artifact | `parity replay ARTIFACT` |
| Verify findings named by a report | `parity evidence verify REPORT` |
| Create/approve intentional-difference policy | `parity budget init`, `parity budget approve` |
| Capture/verify/retire candidate-only contracts | `parity contract ...` |
| Prepare and run managed environments | `parity migration ...` |
| Publish a JSON Schema | `parity schema NAME` |
| Scaffold a Python command adapter | `parity adapter init PATH` |

`check`, `migration check`, `evidence verify` and `contract verify` take a path after `--json`.
`migration init`, `migration validate`, `migration run` and `replay` use boolean `--json` and emit
one machine-readable document to standard output.

## Current boundaries

Parity supports canonical positional/keyword invocations with Arrow/frame and JSON values, Python
callables and arbitrary local command targets.
Returns, raised exceptions, input mutation and process performance are first-class observations.
It does not yet capture and restore filesystem, database, network or subprocess effects itself.

Targets and custom generators execute trusted project code with the authority of the invoking user.
Process separation is a reliability boundary, not a hostile-code sandbox. Counterexamples and
distilled contracts may contain real values even though reports do not. Review the
[security guide](SECURITY.md) before using production-shaped fixtures, untrusted code or CI artifact
uploads.
