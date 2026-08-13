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

Inspect inferred bounds. A sample that contains only positive values can accidentally exclude the
negative domain; a unique-looking sample can accidentally imply uniqueness. Prefer a reviewed
schema in `parity.toml` for important cases.

### 3. Define semantics before tuning

Choose row order, column order, dtype rules and numerical tolerance from the consumer's contract:

- Use `row_order = "strict"` for ranked results, event streams and presentation-ready output.
- Use `row_order = "ignore"` only when rows are genuinely a bag. Duplicate rows are still counted.
- Use `dtype = "strict"` when serialization width, categorical form or nullability matters.
- Use `dtype = "compatible"` when an integer is an integer regardless of storage width.
- Use `null_nan_equal = true` only when a null and an IEEE NaN are interchangeable outcomes.
- Set tolerances from numerical analysis or business limits, not from the first failing example.
- Exclude a column only when it is explicitly non-semantic, such as a generated trace identifier.

Commit policy changes to review. A weakened comparison can hide more defects than a code change.

### 4. Run locally and replay

```bash
parity doctor
parity check --case orders --no-performance
```

On failure, read the mismatch and diagnosis, then replay the preserved input:

```bash
parity replay .parity/orders/<timestamp>-<hash>
```

Current artifacts record the runtime observed inside each worker. Replay first probes both workers
without importing the configured targets. If Python, Parity, platform or a recorded distribution
drifted, neither callable runs and replay returns an error. A legacy artifact without provenance is
still usable, but terminal and Markdown reports mark it unverified rather than claiming an exact
reproduction.

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

Set `max_findings` above one when one campaign should continue searching for other observable
difference shapes. Each distinct signature receives a separate `max_examples` budget. Signatures
are data-free classifications—not bug IDs—and every shrunk witness is confirmed twice; unstable
outputs stop discovery as an error.

Keep seeds in version control for reproducibility, but retain shrinking. Store artifacts with
restricted access because a counterexample derived from a fixture may contain fixture values.

## Callables and environments

Targets use `package.module:function` syntax. The callable receives the input frame as its first
positional argument, followed by `static_args` and `static_kwargs` from the case.

```toml
[cases.reference]
target = "legacy.orders:transform"
adapter = "pandas"
pandas_input = "native"
python = ".venv-legacy/bin/python"
workdir = "."
record_distributions = ["orders-lib", "scikit-learn"]

[cases.candidate]
target = "rewrite.orders:transform"
adapter = "polars"
python = ".venv-candidate/bin/python"
workdir = "."
record_distributions = ["orders-lib", "scikit-learn"]

static_args = ["GBP"]
static_kwargs = { include_tax = true }
```

For a keyword-bound input bundle, each logical name is passed as a frame keyword and cannot collide
with `static_kwargs`; `static_args` are disallowed. Positional bundles pass frames in declared order
before `static_args`. See the [configuration reference](CONFIG_REFERENCE.md) for the relationship
syntax and validation rules.

`adapter = "auto"` is convenient when both functions accept Arrow-compatible input, but explicit
adapters make reviews and errors clearer. A distinct `python` executable lets Parity compare
dependency versions or environments without loading both into one interpreter.

Parity records core dataframe dependencies automatically. Add the library under test and any
dependency whose version is part of the migration contract with `record_distributions`. The two
sides are collected independently, so a reference environment can legitimately report a different
version from the candidate environment. Reports never contain environment values, executable
paths, hostnames or a broad `pip freeze` inventory.

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
    legacy,
    rewrite,
    fixture=sample,
    reference_adapter="pandas",
    candidate_adapter="polars",
    reference_pandas_input="native",
    reference_distributions=["orders-lib"],
    candidate_distributions=["orders-lib"],
)
```

Live joins use `input_fixtures={"orders": orders, "customers": customers}` and optionally matching
`input_schemas`, `relationships`, and `input_binding="keyword"` or `"positional"`. Do not combine
those arguments with the legacy single `fixture`/`schema` pair.

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
its own first observation; matching drift is therefore an error rather than a pass. Configure the
total observations with `generation.stability_repeats` (default `2`, or `1` to disable). If an
invocation times out or crashes, Parity terminates that session and reports an error instead of
restarting it with clean state. Library users who specifically need a new process for each call can
continue to use `parity.execution.execute_isolated`.

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
parity inspect FIXTURE [--output PATH]        infer a portable schema
parity check [--config PATH] [--case NAME]    run campaigns
             [--tag TAG] [--max-examples N]
             [--max-findings N]
             [--stability-repeats N]
             [--performance|--no-performance]
             [--json PATH] [--junit PATH] [--markdown PATH]
parity replay ARTIFACT                        reproduce a counterexample
parity doctor [--json]                        report runtime readiness
parity version                                print the installed version
```

## Current boundaries

The first release supports pandas, Polars and Arrow frames and Python callables. It is designed to
add engines through adapters, but SQL warehouses, Spark clusters, distributed schedulers, GPU
engines and arbitrary side-effect comparison are not present yet. Worker processes isolate
failures; they do not securely sandbox hostile code. See the [roadmap](ROADMAP.md), [architecture](ARCHITECTURE.md)
and [threat model](THREAT_MODEL.md).
