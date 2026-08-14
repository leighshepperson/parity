# User guide

## The verification contract

A Parity case has four parts:

1. A **reference** callable whose observable behaviour is the baseline.
2. A **candidate** callable that should preserve that behaviour.
3. An **input domain**, supplied by a fixture/schema or a named two- or three-frame bundle.
4. An explicit **comparison policy** defining what “the same” means.

Parity converts canonical Arrow input(s) into each callable's requested adapter, observes both
executions, canonicalises the outcomes and compares them. It first runs deterministic adversarial
cases, then a property-based search. A discovered difference is written as a counterexample
artifact; automatic replay is available when the callable environment can be reconstructed.
Generated differences are minimized when shrinking is enabled and succeeds. If semantic checks
pass, Parity can benchmark both implementations.

For joins and lookups, an input bundle generates all frames from one joint strategy. Key overlap,
foreign-key, cardinality and equal-row-count relationships therefore remain true while Hypothesis
shrinks the complete bundle. Mutation is tracked by logical input name and replay restores every
Arrow IPC input atomically.

The reference is not declared *correct* by Parity. It is the contract you have chosen to preserve.
Review reference defects before turning a historical accident into a permanent requirement.

## Recommended migration workflow

### 1. Establish the public boundary

Verify transformations at boundaries meaningful to their users. Good boundaries accept a frame
and return a frame, series or JSON-like value without reading hidden global state. Wrap functions
that currently fetch data, inspect the clock or write files so those effects are explicit static
arguments or test fixtures.

Start with transformations whose wrong answers have a clear cost: billing, allocation, risk,
entitlements, experimentation metrics or published aggregates. Do not begin with every helper in
the repository.

### 2. Capture representative seeds

Use a small, non-sensitive Parquet fixture that contains real structural features: nullable keys,
duplicate keys, empty strings, time zones, categories and realistic dtypes. Parity uses fixtures as
deterministic cases and can infer a broader generation schema from them.

```bash
parity inspect tests/fixtures/orders.parquet --output orders-schema.json
```

The command writes a review-oriented JSON model; it is not a TOML include. Translate the accepted
columns and bounds into the case's `[cases.schema]` section. Inspect inferred bounds. A sample that contains only positive values can accidentally exclude the
negative domain; a unique-looking sample can accidentally imply uniqueness. Prefer a reviewed
schema in `parity.toml` for important cases.

### 3. Define semantics before tuning

Choose row order, column order, dtype rules and numerical tolerance from the consumer's contract:

- Use `row_order = "strict"` for ranked results, event streams and presentation-ready output.
- Use `row_order = "keyed"` with `row_keys = ["account_id", "event_id"]` when rows may move but
  have a unique, stable business identity. Parity aligns the composite key before comparing cells.
- Use `row_order = "ignore"` only when rows are genuinely a bag. Duplicate rows are still counted.
- Use `dtype = "strict"` when serialization width, categorical form or nullability matters.
- Use `dtype = "compatible"` when an integer is an integer regardless of storage width.
- Use `null_nan_equal = true` only when a null and an IEEE NaN are interchangeable outcomes.
- Set tolerances from numerical analysis or business limits, not from the first failing example.
- Exclude a column only when it is explicitly non-semantic, such as a generated trace identifier.

Commit policy changes to review. A weakened comparison can hide more defects than a code change.

Key columns belong to the output contract, not the generated input schema. They must exist on both
outputs and form a unique composite key on each side; do not choose a merely convenient column that
can duplicate. Missing-value and signed-zero equality switches apply to key identity, but numeric
and datetime tolerances never pair distinct keys. Those tolerances still apply to cells after
alignment. Prefer keyed comparison over `ignore` whenever a real output key exists: failures then
point to the differing cell instead of an unmatched whole row.

### 4. Run locally and replay

```bash
parity doctor
parity check --case orders --no-performance
```

On failure, read the mismatch and diagnosis, then replay the preserved input:

```bash
parity replay .parity/orders/<timestamp>-<hash>
```

Replayable artifacts record the effective configuration and runtime observed inside each worker.
Replay first probes both workers without importing the configured targets. If Python, Parity,
platform or a recorded distribution drifted, neither callable runs and replay returns an error.
Evidence without a complete runtime and configuration binding is inspectable but cannot be replayed
automatically.

When a JSON suite or migration report retains several failures, verify all of its referenced
artifacts in report order:

```bash
parity evidence verify .parity/migration-status.json \
  --json .parity/evidence-status.json
```

By default, a report anywhere beneath its reported artifact directory is self-locating. Otherwise,
Parity looks for that directory below the current directory. Use `--artifact-root PATH` when the
same directory has been restored elsewhere. Exit `0` means every finding replayed with the same
mismatch signature, `1` means at least one valid finding is stale (it now passes or produces a
different mismatch shape), and `2` means report, artifact, runtime or execution evidence could not
be verified. Terminal and JSON output include a bounded, data-safe reason code for every stale or
errored artifact, without echoing a private path or caught exception.
Parity verifies integrity and behavioural reproduction;
the `ms1:...` mismatch classifier is not a cryptographic signature or source attestation. Replay
executes the configured project code, so verify only artifacts and checkouts you trust.

The saved Arrow input is the authority. A Parquet convenience copy is also written when its
format can represent the input schema. A diagnosis is a deterministic hypothesis, not an automatic
repair instruction. Add the artifact input (or an appropriately sanitized equivalent) to your own
unit-test corpus before fixing the candidate.

### 5. Separate correctness from speed

Enable performance only after the semantic campaign passes. Benchmarking interleaves reference and
candidate invocations and reports median elapsed time and process peak RSS. Shared CI runners are
noisy; gate only large, repeatable regressions there. Use a controlled runner and representative
fixture for serious performance decisions.

`fail_on_regression = false` records evidence without failing a migration. Turn it on only after a
stable baseline exists.

### 6. Gate pull requests

Run quick critical cases on every pull request and a larger campaign on a schedule:

```bash
parity check --tag critical --max-examples 250 --no-performance
parity check --max-examples 5000 --json .parity/nightly.json
```

When `--case` and `--tag` are supplied together, Parity runs their union. It never treats the tag
as a further filter on the explicitly named cases.

Set `max_findings` above one when one campaign should continue searching for other observable
difference shapes. Each distinct signature receives a separate `max_examples` budget. Signatures
are data-free classifications—not bug IDs—and every shrunk witness is confirmed twice; unstable
outputs stop discovery as an error.

Keep seeds in version control for reproducibility, but retain shrinking. Store artifacts with
restricted access because a counterexample derived from a fixture may contain fixture values.

### 7. Gate the declared migration surface

A set of passing cases does not say which APIs were never considered. Keep a separate, reviewed
`migration.toml` that maps every declared migration unit to its evidence:

```toml
version = 1

[[units]]
id = "orders-transform"
cases = ["orders-control", "orders-null-keys", "orders-empty"]

[[units]]
id = "customer-summary"

[[units]]
id = "plot-orders"
excluded_reason = "Presentation-only figure output is outside this migration."
```

`customer-summary` is uncovered: it is valid while work is in progress but makes the gate fail.
An exclusion cannot also list cases, and an exclusion reason must contain non-whitespace text.

```bash
parity migration check \
  --manifest migrations/migration.toml \
  --config migrations/parity.toml \
  --json migrations/.parity/migration-status.json
```

The command validates every mapped case name before executing project code, runs the union of
mapped cases once and attempts the complete union even if `parity.toml` sets `fail_fast = true`.
Several units may share a case; unrelated exploratory cases in `parity.toml` are not selected.
There are deliberately no case, tag, generation-budget or performance overrides on the migration
gate. Use ordinary `parity check --case ... --no-performance` commands while iterating, then let the
final gate enforce the committed configuration.

Each unit is derived as `passed`, `failed`, `error`, `excluded` or `uncovered`. A mapped unit is an
error when a case errors, is skipped, produces zero observations or is unexpectedly absent; it can
never pass without execution evidence. Overall exit codes are:

- `0`: at least one unit passed and every other unit passed or was explicitly excluded;
- `1`: a unit failed or remained uncovered, or the entire manifest was excluded; and
- `2`: the manifest/configuration was invalid, a mapped case was unknown, or execution evidence was
  errored or incomplete.

Error takes precedence over failure. The JSON output uses migration report schema version 1 and
contains derived counts, per-unit case status, a canonical manifest hash and the data-safe Parity
report for the selected case union. That nested report carries the effective configuration
hash. Reports omit compared values and redact unit identifiers, case names and exclusion reasons;
counterexample artifacts still contain actual fixture-derived or generated inputs.

The ledger proves completion only against its declared inventory. It cannot discover an omitted API
or establish that a mapped case genuinely exercises the unit named in the ledger. Review public
exports, documentation, signatures, upstream tests, wrappers and exclusions. The
[agent migration protocol](AGENT_MIGRATION.md) provides a complete inventory-to-release workflow.

## Managed migration workspaces

Keep migration wrappers and declarations in `migrations/`, outside the candidate package's import
root. Start with one fixture-backed core case:

```bash
python -m pip install "parity-check[workspace]"
parity init migrations/parity.toml \
  --reference your_library.api:transform \
  --candidate your_library.api:transform \
  --fixture tests/fixtures/input.parquet
parity migration init --reference 'your-library==1.2.3'
parity migration run
```

`migration init` writes `migrations/parity.workspace.toml`. If the configured ledger is absent, it
also creates `migrations/migration.toml` with one `core-regression` unit mapped to every configured
case; review that inventory before treating it as a completion gate. The exact released requirement
supplies the reference package and the current checkout is installed as the candidate.

`migration run` resolves a hash-pinned requirements lock, creates both isolated workers, enforces
the exact reference distribution before target import, records the subject distribution on both
sides, runs the complete ledger and writes
`migrations/.parity/workspace/reports/default.json`. Re-running uses the existing lock; pass
`--refresh-locks` only for a deliberate dependency update. `parity migration setup` prepares the
same workers without executing project callables.

tox owns the environment lifecycle, tox-uv supplies uv-backed environments, and uv resolves the
locks. These are implementation details behind the Parity commands. Parity does not clone a
repository, choose a source revision, apply a patch or change candidate source. Prepare and review
the checkout before running it.

The managed path also verifies that the candidate's statically declared distribution name matches
the exact reference requirement. Supported declarations are `project.name`, `tool.poetry.name`,
and `setup.cfg` `[metadata] name`. For dynamic `setup.py`-only metadata, provision the two worker
interpreters yourself and keep using the explicit `reference.python` and `candidate.python` fields.

### Rolling A→B→C migrations

The workspace represents exactly one active adjacent pair. It is not a migration-history database.
For A→B, keep durable checks in a `core-regression` unit and add units or cases specific to that
transition. Once B is published and becomes the next baseline:

```bash
parity migration advance --reference 'your-library==B_VERSION'
# update or remove only the B→C-specific cases and manifest units
parity migration run
```

`advance` preserves the candidate checkout, Python, lanes, case config, ledger and report location.
It atomically changes only the exact reference and invalidates the previous active lane reports.
The next run may retain compatible transitive pins; use `--refresh-locks` only when dependency
selection itself should change. Old counterexample directories are inert local evidence and may be
deleted when the previous transition is no longer needed.

Only cases mapped by `migration.toml` contribute to completion. Keeping a case in `parity.toml`
without retaining its manifest mapping does not run or certify it. A typical rolling ledger keeps
this permanent unit and replaces the remaining units per hop:

```toml
version = 1

[[units]]
id = "core-regression"
cases = ["import-smoke", "public-contract", "nullable-inputs"]

[[units]]
id = "b-to-c-transition"
cases = ["new-engine-path", "changed-default"]
```

Managed workers execute from the directory containing `parity.workspace.toml`. This prevents a
flat-layout candidate checkout from shadowing the installed reference package. Put wrapper modules
in `migrations/`; import the candidate through its editable installation, not by adding the project
root to `PYTHONPATH`. Parity rejects a managed layout that exposes the candidate checkout to the
reference worker.

Add repeatable dependency lanes when the same migration must pass under more than one constraints
file:

```bash
parity migration init \
  --reference 'your-library==1.2.3' \
  --candidate . \
  --lane minimum=requirements/minimum.txt \
  --lane current=requirements/current.txt
parity migration run
```

Each lane receives its own lock, reference/candidate workers and JSON report. Lane requirement
files are resolver inputs and may use ordinary compatible ranges; the generated lock records the
exact transitive result with hashes. The configured reference and candidate targets, input domain,
comparison policy and migration inventory stay identical across lanes.

The workspace is optional. Set `reference.python` and `candidate.python` explicitly for externally
provisioned runners or environment systems that Parity must not own.

### Reusing cases without hiding the contract

Large migrations can move case declarations into one contained, non-recursive file and keep bounded
defaults at the root:

```toml
version = 1
cases_file = "cases.toml"

[case_defaults.reference]
workdir = "."

[case_defaults.candidate]
workdir = "."

[case_defaults.generation]
max_examples = 500
stability_repeats = 2
```

`cases.toml` contains only `version = 1` and one or more `[[cases]]`. A case overrides a default;
nested tables merge while lists replace. Defaults deliberately cannot hide names, targets, inputs,
tags or shared invocation arguments. Paths still resolve beside the root `parity.toml`, and the
expanded configuration is what Parity validates and fingerprints.

## Callables and environments

Targets use `package.module:function` syntax. The callable receives the input frame as its first
positional argument, followed by `static_args`. Both sides receive `static_kwargs`; the reference
also receives `reference_kwargs` and the candidate receives `candidate_kwargs`.

`parity init` can write a minimal fixture-backed case directly. The
three project inputs are all-or-none, and the command validates the target syntax, adapters and
fixture before atomically writing the configuration:

```bash
parity init migrations/parity.toml \
  --reference orders.reference:transform \
  --candidate orders.candidate:transform \
  --fixture tests/fixtures/orders.parquet \
  --case-name orders \
  --reference-adapter pandas \
  --candidate-adapter polars \
  --record-distribution orders-lib \
  --row-key order_id
```

Repeat `--record-distribution` or `--row-key` for additional names. Distribution names are recorded
on both sides, while adapters and Python executables are configured per side. With no project
options, `parity init` retains the editable starter behaviour and creates `parity_example.py`.

### Explicit environment setup

The managed workspace is the recommended setup for a declared library migration. The same import
target can also run in two environments prepared by another tool. For example, this compares one
Polars wrapper under two releases while keeping the interpreter paths explicit:

```bash
python -m venv .venv-polars-reference
python -m venv .venv-polars-candidate
PARITY_RELEASE="$(parity version)"
.venv-polars-reference/bin/python -m pip install "parity-check==$PARITY_RELEASE" polars==1.0.0
.venv-polars-candidate/bin/python -m pip install "parity-check==$PARITY_RELEASE" polars==1.41.1

parity init parity.toml \
  --reference project.polars_transform:run \
  --candidate project.polars_transform:run \
  --fixture tests/fixtures/input.parquet \
  --reference-adapter polars --candidate-adapter polars \
  --reference-python .venv-polars-reference/bin/python \
  --candidate-python .venv-polars-candidate/bin/python \
  --record-distribution polars

parity doctor --config parity.toml
parity check --config parity.toml
```

Reading `PARITY_RELEASE` from the installed controller keeps the guide current while ensuring both
isolated workers install the exact same Parity release. Each configured worker must carry its own
installation; `parity doctor --config parity.toml` verifies the observed versions before execution.

`doctor --config` asks each worker only for bounded runtime provenance; it does not import or invoke
the configured target. “Ready” therefore means the worker runtime contract is ready, not that a
target import has succeeded. Its terminal and JSON output place reference and candidate Python, Parity
and explicitly requested distribution versions side by side without executable paths, working
directories or environment values. A worker failure or missing requested distribution returns exit
code 2. Use `--case NAME` to inspect one case in a multi-case file.

```toml
[[cases]]
name = "orders"
fixture = "tests/fixtures/orders.parquet"
static_args = ["GBP"]
static_kwargs = { include_tax = true }
reference_kwargs = { engine = "pandas" }
candidate_kwargs = { engine = "polars" }

[cases.reference]
target = "orders.reference:transform"
adapter = "pandas"
pandas_input = "native"
python = ".venv-reference/bin/python"
workdir = "."
record_distributions = ["orders-lib", "scikit-learn"]
required_distributions = { orders-lib = "==1.2.*", pandas = ">=2,<3" }

[cases.candidate]
target = "orders.candidate:transform"
adapter = "polars"
python = ".venv-candidate/bin/python"
workdir = "."
record_distributions = ["orders-lib", "scikit-learn"]
required_distributions = { orders-lib = ">=2,<3", polars = ">=1,<2" }
```

For a keyword-bound input bundle, each logical name is passed as a frame keyword and cannot collide
with `static_kwargs`, `reference_kwargs` or `candidate_kwargs`; `static_args` are disallowed.
Positional bundles pass frames in declared order before `static_args`. Shared and side-specific
keyword names may not overlap. This lets one wrapper receive `engine="pandas"` and the other
`engine="polars"` without duplicating otherwise identical wrapper functions or cases. See the
[configuration reference](CONFIG_REFERENCE.md) for the relationship syntax and validation rules.

`adapter = "auto"` is convenient when both functions accept Arrow-compatible input, but explicit
adapters make reviews and errors clearer. A distinct `python` executable lets Parity compare
dependency versions or environments without loading both into one interpreter.

Parity records core dataframe dependencies automatically. Add the library under test and any
dependency whose version is part of the migration contract with `record_distributions`. The two
sides are collected independently, so a reference environment can legitimately report a different
version from the candidate environment. Reports never contain environment values, executable
paths, hostnames or a broad `pip freeze` inventory.

Use `required_distributions` when a version is a prerequisite rather than provenance alone. It maps
normalized distribution names to PEP 440 specifiers. Before importing or invoking the target,
Parity probes the worker and returns a `RuntimeContractError` if a named distribution is missing,
unavailable or outside its range. Required names are recorded automatically. Every configured
worker must also run the exact Parity version used by the controller; that requirement is automatic
and does not need a config entry. `parity doctor --config parity.toml` reports both the requested
range and whether the observed version satisfies it.

Pandas callables receive Arrow-backed pandas dtypes by default because that preserves the canonical
input's nullable integers and its distinction between null and IEEE NaN. Set
`pandas_input = "native"` only when an implementation relies on pandas' conventional
NumPy/object materialization. Native conversion can widen nullable integers and merge null with
NaN, and its exact dtypes can change between pandas versions. The selected mode is part of the
callable contract saved in failure replays; runs made with different modes are not the same input
contract.

The live Python API has the matching `reference_adapter` and `candidate_adapter` keyword
arguments, plus `reference_pandas_input` and `candidate_pandas_input`. Set them explicitly for
unannotated functions that consume different dataframe types or pandas dtype conventions. The
matching `reference_distributions` and `candidate_distributions` arguments record target package
versions for live checks:

```python
result = parity.verify(
    reference_transform,
    candidate_transform,
    fixture=sample,
    reference_adapter="pandas",
    candidate_adapter="polars",
    reference_pandas_input="native",
    reference_distributions=["orders-lib"],
    candidate_distributions=["orders-lib"],
)
```

Live joins use `input_fixtures={"orders": orders, "customers": customers}` and optionally matching
`input_schemas`, `relationships`, and `input_binding="keyword"` or `"positional"`. These bundle
arguments are mutually exclusive with the single-frame `fixture`/`schema` pair.

When both live callables are plain module-level functions with stable import paths, failure
artifacts preserve the full comparison contract and can be replayed from the same project checkout.
Lambdas, nested functions, bound methods, callable instances and functions defined in `__main__`
still produce saved evidence, with generated failures minimized when shrinking succeeds, but cannot
be re-imported by a later process; use a configured case when automatic replay is required.

Live verification runs sequentially in the caller's interpreter; replay and configured campaigns
isolate each side in a separate process. Before accepting a configured finding, or a live finding
whose two callables have stable import paths, Parity re-observes the witness in newly started worker
processes. A stateful result that disappears in clean execution is reported as an error, never
accepted as a semantic finding. Non-importable live callables are repeated only in the caller's
process, must be pure functions of their arguments, and produce evidence that cannot be replayed
automatically. Project-relative interpreter and import paths can be preserved. Absolute or external
interpreter and import paths are deliberately omitted rather than replaced with the current
environment, so those artifacts remain inspectable evidence but require an explicit configuration
to re-run.

A configured case uses one persistent reference worker and one persistent candidate worker for the
whole semantic campaign and its benchmark. The two sides never share a process, and every call
receives a freshly deserialized input. Python module globals and other process state do persist
between examples on each side. Avoid call counters, mutable caches with observable behaviour,
background threads and other hidden state: they can make generated search and shrinking depend on
execution order. Parity repeats deterministic passing inputs and compares each implementation with
its own first observation using exact, reflexive canonical identity. Cross-side tolerances,
row alignment and null/NaN inequality do not turn a stable output into a nondeterminism error;
matching actual drift is still an error rather than a pass. Configure the
total observations with `generation.stability_repeats` (default `2`, or `1` to disable). If an
invocation times out or crashes, Parity terminates that session and reports an error instead of
restarting it with clean state. Use `parity.execution.execute_isolated` when every call requires a
fresh process.

The `environment` table contains literal environment overrides. Do not put credentials in
`parity.toml`; inject secrets through the CI runner if a wrapper truly needs them. Prefer pure,
offline wrappers.

## Fixture and schema strategy

Use both when possible:

- A fixture catches application-specific structure and serves as the performance input.
- A schema makes allowed values, nullability, bounds and constraints explicit.
- Column `examples` force useful domain values into deterministic probes.
- `categories` restrict generation to an enumeration.
- `unique` and `unique_together` prevent impossible duplicates.
- `sorted_by` keeps complete frames in a declared lexicographic order for as-of joins and windows.
- `row_comparison` expresses per-row valid domains such as `start <= end` without wrapper-side
  filtering.

Generation budgets count classifier evaluations plus confirmation runs. Deterministic adversarial inputs—fixture, empty,
singleton, null, NaN/signed-zero, duplicate, extreme, temporal, categorical and reversed-order
cases where applicable—are reported separately. Each deterministic input contributes one to the
reported example count even when stability checking invokes both implementations more than once.
For a reviewed fixture whose exact rows are the complete contract, set `generation.search = false`
and `adversarial_examples = false`. Parity still checks that fixture and its stability repeats, but
does not search the inferred schema or launch Hypothesis. A searchless case with no deterministic input is
rejected rather than reported as a pass.

## Interpreting outcomes

`passed` means every observed reference/candidate outcome satisfied the selected policy. `failed`
means Parity found a semantic mismatch or an enforced performance regression. `error` means the
campaign itself could not produce reliable evidence, for example because of invalid configuration,
import failure, timeout or corrupt artifact.

When both implementations raise exceptions, `check_exceptions` controls whether equivalent
failure behaviour is required. An exception match is evidence only for that input; it is not a
successful business result. Input mutation is checked separately because two equal return values
can leave materially different downstream state.

## Useful commands

```text
parity init [PATH] [--force]                  create a runnable starter
  --reference TARGET --candidate TARGET       scaffold an existing project pair
  --fixture FILE [--case-name NAME]
  [--reference-adapter NAME] [--candidate-adapter NAME]
  [--reference-python PATH] [--candidate-python PATH]
  [--record-distribution NAME] [--row-key COLUMN]
parity inspect FIXTURE [--output PATH]        infer a portable schema
parity check [--config PATH] [--case NAME]    run campaigns
             [--tag TAG] [--max-examples N]
             [--max-findings N]
             [--stability-repeats N]
             [--performance|--no-performance]
             [--json PATH] [--junit PATH] [--markdown PATH]
parity migration check --manifest PATH        gate the declared migration inventory
                       --config PATH [--json PATH]
parity migration init --reference PACKAGE==VERSION
                      [--candidate PATH]       declare a managed workspace
                      [--lane NAME[=REQUIREMENTS]]
parity migration advance --reference PACKAGE==VERSION
                                              move the active adjacent pair
parity migration setup [--workspace PATH]     prepare locked worker environments
                       [--refresh-locks]
parity migration run [--workspace PATH]       prepare and run every dependency lane
                     [--refresh-locks]
parity evidence verify REPORT                 replay report-referenced findings
                       [--artifact-root PATH] [--json PATH]
parity replay ARTIFACT                        reproduce a counterexample
parity doctor [--json]                        report runtime readiness
parity doctor --config PATH [--case NAME]     inspect configured workers
              [--json]
parity version                                print the installed version
```

## Current boundaries

Parity currently supports pandas, Polars and Arrow frames and Python callables. It is designed to
add engines through adapters, but SQL warehouses, Spark clusters, distributed schedulers, GPU
engines and arbitrary side-effect comparison are not present yet. Worker processes isolate
failures; they do not securely sandbox hostile code. See the [roadmap](ROADMAP.md),
[architecture](ARCHITECTURE.md) and [threat model](THREAT_MODEL.md).
