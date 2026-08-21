# User guide

## The verification contract

A Parity case has four parts:

1. A **reference** target whose observable behaviour is the baseline.
2. A **candidate** target that should preserve that behaviour.
3. An **input domain**, supplied by a fixture/schema or a named two- or three-frame bundle.
4. An explicit **comparison policy** defining what “the same” means.

Targets may be Python callables in unrelated environments or arbitrary protocol-speaking commands.
Parity sends each one the same canonical Arrow input(s), observes semantic Return/Raise outcomes,
canonicalises successful values and compares them. It first runs deterministic adversarial cases,
then a property-based search. A discovered difference is classified and written as a
counterexample artifact; automatic replay is available when the target contract can be
reconstructed. Generated differences are minimized when shrinking is enabled and succeeds. If
semantic checks pass, Parity can benchmark both implementations.

For joins and lookups, an input bundle generates all frames from one joint strategy. Key overlap,
foreign-key, cardinality and equal-row-count relationships therefore remain true while Hypothesis
shrinks the complete bundle. Mutation is tracked by logical input name and replay restores every
Arrow IPC input atomically.

The reference is not declared *correct* by Parity. It is the contract you have chosen to preserve.
Review reference defects before turning a historical accident into a permanent requirement.

For the common path, initialize once and let the default command run the complete configured suite:

```bash
parity init
parity check
```

The base controller and generated starter are Arrow-only. Install `parity-check[pandas]` or
`parity-check[polars]` only when a controller-side case selects that adapter; missing optional
adapters produce an exact install hint. Managed target environments still receive their adapter
dependencies from the subject package or lane requirements.

The remaining sections explain how to make that contract accurate; they are not required setup
steps for every case.

## Recommended migration workflow

### 1. Establish the public boundary

Verify behaviour at boundaries meaningful to users. A good boundary accepts canonical data and
returns a frame or JSON-like value without hidden global state. A small adapter may unpack that data
into an old function signature, construct new domain objects, or canonicalise a new return type;
reference and candidate APIs do not need to match. Project effects such as clock, file or service
inputs into explicit fixtures where practical.

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
parity doctor --config parity.toml
parity check --case orders --no-performance
```

On failure, read the mismatch and diagnosis, then replay the preserved input:

```bash
parity replay .parity/orders/<timestamp>-<hash>
```

A reproduced incompatibility remains `FAILED`, so `parity replay` exits `1` when it successfully
recreates the saved mismatch. This preserves the ordinary `PASSED`/`FAILED`/`ERROR` CLI contract.
Use `parity evidence verify` when successful reproduction itself should exit `0`.

Replayable artifacts record the effective configuration and runtime observed inside each target.
Replay first probes both transports/runtimes and declared requirements without importing configured
targets. Only when both succeed does it probe endpoint imports/configuration, still without invoking
application behaviour. If a bound runtime, platform, distribution or source identity drifted,
neither target runs and replay returns an error.
Evidence without a complete runtime and configuration binding is inspectable but cannot be replayed
automatically.

Replay v2 records a bounded ancestor from the artifact back to the directory containing the loaded
`parity.toml`. Resolution begins at the artifact and never consults the shell's current directory,
so an absolute artifact path can be replayed from anywhere. For example:

```bash
parity replay /path/to/project/migrations/.parity/<case>/<finding-directory>
```

Use module-level import targets and keep configured interpreters, workdirs and path-like command
executables inside that configuration directory. A live callable or external/missing path still
produces inspectable evidence, but cannot be reconstructed automatically. Replay identifies the
affected side and explains whether to author an import target, create a configuration-local virtual
environment, move a workdir/executable inside the configuration directory or rerun after recreating
a missing executable.
For managed execution, the workspace directory and its generated environments must therefore be
contained by the configuration directory. The default layout places both TOML files in
`migrations/` and satisfies this automatically.

When a JSON suite or migration report retains several failures, verify all of its referenced
artifacts in report order:

```bash
(cd migrations && parity evidence verify .parity/migration-status.json \
  --json .parity/evidence-status.json)
```

By default, a report anywhere beneath its reported artifact directory is self-locating. Otherwise,
Parity looks for that directory below the current directory. Use `--artifact-root PATH` when the
same directory has been restored elsewhere. Exit `0` means every finding replayed with the same
mismatch signature, `1` means at least one valid finding is stale (it now passes or produces a
different mismatch shape), and `2` means report, artifact, runtime or execution evidence could not
be verified. Terminal and JSON output include a bounded, data-safe reason code for every stale or
errored artifact, without echoing a private path or caught exception. When collection itself cannot
preserve an executable replay contract, `replay.json` retains the affected side in an optional
`replay_blockers` map using `live_callable`, `external_python`, `external_workdir`,
`external_command` or `missing_command`.
Parity verifies integrity and behavioural reproduction;
the `ms3:...` mismatch classifier is not a cryptographic signature or source attestation. Replay
executes the configured project code, so verify only artifacts and checkouts you trust.

The saved Arrow input is the authority. A Parquet convenience copy is also written when its
format can represent the input schema. A diagnosis is a deterministic hypothesis, not an automatic
repair instruction. Add the artifact input (or an appropriately sanitized equivalent) to your own
unit-test corpus before fixing the candidate.

### 5. Separate correctness from speed

Enable performance only after the semantic campaign passes. Benchmarking interleaves paired
reference and candidate invocations, reports median elapsed time and process peak RSS, and computes
a deterministic bootstrap confidence interval for each ratio. A threshold is exceeded only when
the interval's lower bound is beyond it, so one noisy timing cannot fail a gate. Shared CI runners
are still noisy; gate only large, repeatable regressions there. Use a controlled runner and a
representative fixture for serious performance decisions. If performance is enabled but Parity has
no validated passing input, the benchmark runner is unavailable, or an invocation cannot complete,
the case is `ERROR`; requested performance evidence is never silently omitted from a pass.

`fail_on_regression = false` records evidence without failing a migration. Turn it on only after a
stable baseline exists; an enforced gate requires at least five measured pairs and defaults to nine.

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

For domain objects that do not fit the compact schema vocabulary, point a case at project-owned
generation code instead of expanding `parity.toml` into another schema language:

```toml
[[cases]]
name = "portfolio"

[cases.reference]
target = "migration_adapters:legacy_portfolio"

[cases.candidate]
target = "migration_adapters:new_portfolio"

[cases.generation]
generator = "tests.generators:portfolios"
max_examples = 500
seed = 20260820
```

```python
from hypothesis import strategies as st
import pandas as pd


def portfolios():
    return st.lists(
        st.tuples(st.integers(1, 20), st.floats(-1_000, 1_000)),
        max_size=20,
    ).map(lambda rows: pd.DataFrame(rows, columns=["asset_id", "value"]))
```

The factory runs in the Parity driver and returns the strategy; reference and candidate still run
in their separate environments. Hypothesis shrinking is retained. A factory may instead return a
plain iterable, which is useful for an existing domain corpus but is only bounded/replayed—not
automatically shrunk.

Run independent cases concurrently once the suite contains enough work to benefit:

```bash
parity check --jobs 8 --native-threads 1
```

Results stay in case declaration order. Search and shrinking inside each case remain serial.
`--native-threads` is opt-in; use it for NumPy/BLAS/OpenMP-heavy targets to avoid multiplying case
processes by native thread pools. Concurrent jobs cannot be combined with `fail_fast = true` or an
enforced performance gate: competing target cases would make stop order or benchmark evidence
scheduler-dependent.
Report-only performance measurements are permitted with concurrent jobs, but the cases still
contend for one host. Use `jobs = 1` for performance evidence you intend to compare or retain.

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

Keep migration wrappers and declarations in `migrations/`, outside either package's import root. For
a simple published-package upgrade, one command creates a fixture-backed case, starter ledger and
managed workspace:

```bash
python -m pip install "parity-check[workspace]"
parity migration init \
  --reference-package 'your-library==1.2.3' \
  --candidate-package 'your-library==2.0.0' \
  --scaffold \
  --json
parity migration validate --json
parity migration run --json
```

`--scaffold` writes a deliberately incomplete Arrow wrapper, tiny JSON fixture, config, starter
ledger and four-item review checklist. It refuses to overwrite any authored file, even with
`--force`. Review the wrapper, fixture, ledger and comparison policy, then mark every checklist item
and its top-level status `resolved`. `migration validate` loads workspace v3, config, ledger and
fixtures without creating `.parity` state or invoking either package. It exits `1` for unresolved
review work and `2` for an invalid contract.

For an existing callable and fixture, `--target` supplies the same import target on both sides. Use
`--reference-target` and
`--candidate-target` when the release changes its import path or wrapper. The scaffolder also accepts
`--reference-adapter`, `--candidate-adapter`, repeatable `--record-distribution` and repeatable
`--row-key`. `--fixture` is interpreted from the command's current directory and stored relative to
the generated `parity.toml`. In this explicit-target mode, a target names an existing importable
callable and initialization does not generate its implementation. Managed wrapper modules are
imported from the workspace directory. Before publishing, the command validates target spelling,
adapter values, fixture readability and the resulting configuration; it does not import the target.
`migration setup` subsequently validates package resolution and editable origins, while `migration
run` also preflights configured target imports. The starter ledger contains one `core-regression`
unit mapped to every configured case.

Treat the generated targets, adapters, fixture boundary, comparison policy and ledger as reviewed
code. Scaffolding does not establish that they describe the whole public contract. If
`migrations/parity.toml` already exists, omit `--target`, `--fixture` and the other case-scaffolding
options: `migration init` loads the existing contract and refuses to overwrite it. `--force`
replaces only `parity.workspace.toml`; it never replaces the reviewed case configuration.

Each side independently accepts an exact released package or an existing local checkout:

| Direction | Reference declaration | Candidate declaration |
|---|---|---|
| released→released | `--reference-package package==1.2.3` | `--candidate-package package==2.0.0` |
| released→local | `--reference-package package==1.2.3` | `--candidate-path ../candidate` |
| local→released | `--reference-path ../reference` | `--candidate-package package==2.0.0` |
| local→local | `--reference-path ../reference` | `--candidate-path ../candidate` |

The package form must be one unconditional, non-wildcard `==` requirement; extras such as
`package[io]==2.0.0` are supported. The reference requires exactly one form. Candidate package/path
flags are mutually exclusive; omitting both is shorthand for `--candidate-path .`. The saved
version 3 workspace uses only `reference_package` / `reference_path` and `candidate_package` /
`candidate_path` as source keys, with exactly one key from each pair.

For main-versus-branch, worktree-versus-worktree or local-refactor regression testing, use the same
workflow with two existing checkouts:

```bash
parity migration init \
  --reference-path ../main-worktree \
  --candidate-path ../feature-worktree
parity migration run
```

Parity uses local sources in place; it never creates, switches, resets or edits a worktree.

The shared target interpreter defaults to the controller's Python. When the migration also changes
Python, declare each side directly:

```bash
parity migration init \
  --reference-path ../main-worktree \
  --candidate-path ../feature-worktree \
  --reference-python 3.8 \
  --candidate-python 3.12
```

Managed target interpreters may be Python 3.8 or newer and receive the target package plus the
PyArrow protocol transport, not the full Parity installation. A pandas or Polars adapter also
needs that library on its side; declare it in the subject package or lane requirements when it is
not already a dependency. The Parity controller remains on Python 3.11 or newer.

`migration run` resolves a separate hash-pinned requirements lock for each target, creates both
isolated targets, binds every exact released requirement to its corresponding target before import,
records the subject distribution on both sides, runs the complete ledger and writes
`migrations/.parity/workspace/reports/default.json`. Re-running uses the existing locks; pass
`--refresh-locks` only for a deliberate dependency update. `parity migration setup` prepares the
same environments without executing project callables. A case-level `required_distributions`
constraint that excludes either workspace version is rejected instead of silently weakening the
exact package contract.

During `migration run`, every local side is installed editable only in its own environment and its
installed distribution metadata and importable modules must resolve to the declared source. Each
local target reports a path-free Git HEAD, dirty flag and deterministic source digest; findings and
replay bind that target identity. A local source must therefore be a Git worktree with a committed
HEAD. Dirty worktrees are allowed and explicitly bound by the digest. The workspace directory and
`PYTHONPATH` may not expose a managed checkout directly.

Local/local runs add a stronger paired driver contract. Before setup, after setup and around every
dependency lane, Parity snapshots both worktrees and invalidates all active results if either source
changes. It checks that each target identity matches those snapshots and writes the relocatable
`source-provenance.json` beside the lane reports. That file contains hashes and state only, never
checkout paths, branch names or file names. Released/local and local/released runs retain the one
local target identity in runtime/finding/replay evidence, but do not produce this paired report or
the continuous two-worktree driver checks.

The environment builder and resolver are implementation details behind `migration setup/run`.
Users do not need to author a separate environment-runner configuration. Target environments install
the application and PyArrow for the portable protocol worker; package metadata and lane requirements
supply any pandas, Polars or other adapter dependency. They do not install the full Parity
application or its controller dependencies.

The managed path verifies that every local checkout's statically declared distribution name matches
the shared subject name. Supported declarations are `project.name`, `tool.poetry.name`, and
`setup.cfg` `[metadata] name`. For dynamic `setup.py`-only metadata, provision the two target
interpreters yourself and use the optional unmanaged path with explicit `reference.python` and
`candidate.python` fields.

### Rolling A→B→C released migrations

The workspace represents exactly one active adjacent pair. It is not a migration-history database.
For A→B, keep durable checks in a `core-regression` unit and add units or cases specific to that
transition. Once B is published and becomes the next baseline:

```bash
parity migration advance --reference-package "$NEXT_REFERENCE_PACKAGE_SPEC"
# update or remove only the B→C-specific cases and manifest units
parity migration run
```

`advance` preserves the candidate declaration, Python, lanes, case config, ledger and report location.
It atomically changes only the exact reference and invalidates the previous active lane reports.
If the candidate is also an exact released package, update `candidate_package` separately (or
regenerate the reviewed workspace with `migration init --force`) before running the next pair;
Parity does not guess the next candidate release.
The next run may retain compatible transitive pins; use `--refresh-locks` only when dependency
selection itself should change. Old counterexample directories are inert local evidence and may be
deleted when the previous transition is no longer needed.

`migration advance` is intentionally limited to exact released references. For a local/local pair,
edit the workspace or regenerate it with `migration init --force` and the next reviewed
`reference_path`; Parity never chooses or moves a branch for you.

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

Managed targets execute from the directory containing `parity.workspace.toml`. This prevents a
flat-layout checkout from shadowing an installed package. Put wrapper modules in `migrations/`;
import local subjects through their editable installations, not by adding either project root to
`PYTHONPATH`. Parity rejects a managed layout that exposes a managed checkout directly. It also
requires the directory containing the configured `parity.toml` to contain this workspace directory,
which keeps automatic replay paths local; colocating both files is the default and simplest layout.

Add repeatable dependency lanes when the same migration must pass under more than one constraints
file:

```bash
parity migration init \
  --reference-package "$REFERENCE_PACKAGE_SPEC" \
  --candidate-path . \
  --lane minimum=requirements/minimum.txt \
  --lane current=requirements/current.txt
parity migration run
```

Each lane receives separate reference and candidate locks, two target environments and one JSON result. Lane
requirement files are resolver inputs and may use ordinary compatible ranges; each generated lock
records the exact side-specific transitive result with hashes. The configured targets, input domain,
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

## Targets, adapters and environments

Python targets use `package.module:callable.path` syntax. A single-input callable receives its
adapted frame first, followed by `static_args`. Both sides receive `static_kwargs`; the reference
also receives `reference_kwargs` and the candidate receives `candidate_kwargs`.

The two callables do not need the same signature or architecture. Keep a canonical input contract
and translate it in small, reviewed project adapters:

```python
def reference_calculation(frame):
    from legacy import calculate

    row = frame.iloc[0]
    return calculate(row.x, row.y, row.currency)


def candidate_calculation(frame):
    from rewritten import Data, Engine

    row = frame.iloc[0]
    return Engine(row.currency).calculate(Data(row.x, row.y))
```

In dependency-isolated runs, do not import both implementations at the top of one shared wrapper
module: each target environment may intentionally contain only its own implementation. Keep the
imports inside the side-specific functions as above, or use separate wrapper modules. Preflight
imports the configured module but never invokes either function.

If the raw return types also differ, configure an importable output canonicalizer on either side:

```toml
[cases.candidate]
target = "migration_adapters:candidate_calculation"
canonicalizer = "migration_adapters:calculation_to_contract"
adapter = "pandas"
```

The canonicalizer runs in that target environment after a successful call and returns a supported
Arrow/frame or JSON-like value. It never intercepts a target exception. Target exceptions are
semantic Raise outcomes; adapter import, invocation or output-canonicalisation failures are
infrastructure `ERROR`.

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
target can also run in two environments prepared by any environment manager. Each environment
needs PyArrow, the application/wrappers under test and any dependency required by its selected
adapter—not `parity-check` or the controller's Pydantic/Hypothesis/CLI dependencies:

```bash
python -m venv .venv-reference
python -m venv .venv-candidate
.venv-reference/bin/python -m pip install pyarrow -r requirements/reference.txt
.venv-candidate/bin/python -m pip install pyarrow -r requirements/candidate.txt

parity init parity.toml \
  --reference migration_adapters:reference_transform \
  --candidate migration_adapters:candidate_transform \
  --fixture tests/fixtures/input.parquet \
  --reference-adapter pandas --candidate-adapter polars \
  --reference-python .venv-reference/bin/python \
  --candidate-python .venv-candidate/bin/python \
  --record-distribution project-under-test

parity doctor --config parity.toml
parity check --config parity.toml
```

The controller launches its dependency-light portable worker by file path. `doctor --config`
performs two preflight phases: it first checks Arrow transport, runtime provenance and declared
requirements on both sides without importing user code. Only when both transports succeed does it
check target, canonicalizer and adapter imports, without invoking the target. If one transport
fails, doctor JSON records `status = "not_checked"` and
`error_code = "TargetEndpointNotChecked"` for the deferred peer; terminal output says that the
endpoint was not checked. Its output places bounded reference and candidate
runtime/distribution evidence side by side without executable paths, working directories or
environment values. A transport, import, adapter or requirement failure returns exit code 2. Use
`--case NAME` to inspect one case in a multi-case file.

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
required_distributions = { orders-lib = "==1.*" }

[cases.candidate]
target = "orders.candidate:transform"
adapter = "polars"
python = ".venv-candidate/bin/python"
workdir = "."
record_distributions = ["orders-lib", "scikit-learn"]
required_distributions = { orders-lib = "==2.*" }
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
Parity probes the target runtime and returns a `RuntimeContractError` if a named distribution is
missing, unavailable or outside its range. Required names are recorded automatically. There is no
target-side Parity-version requirement because the portable worker does not import Parity.
`parity doctor --config parity.toml` reports both the requested range and whether the observed
version satisfies it.

An arbitrary local executable can replace a Python target:

```toml
[cases.reference]
command = ["./bin/legacy-contract-adapter"]

[cases.candidate]
command = ["./bin/new-contract-adapter", "--compat"]
```

`command` and `target` are mutually exclusive. A command endpoint owns its runtime, input adapter
and output canonicalisation, so it cannot also set `python`, `adapter`, `pandas_input` or
`canonicalizer`. It receives private Arrow/JSON requests through target protocol v1 and returns
strict Return/Raise/Error observations plus generic runtime identity. This is the first-class path
for Fortran, C/C++, Rust, Java, legacy CLIs and other runtimes.

When Python is a suitable boundary around the external program, scaffold an adapter rather than
writing the protocol lifecycle by hand:

```bash
parity adapter init adapters/legacy.py
```

Implement the generated module's target inspection and `execute` mapping, then use its SDK runner:

```toml
[cases.reference]
command = ["parity", "adapter", "serve", "adapters/legacy.py"]
```

The adapter process needs `parity-check`; the wrapped external target does not. The SDK handles
private paths, Arrow/JSON transport, persistent sessions and atomic responses. It exposes explicit
`Return`, `TargetRaised` and `AdapterError` outcomes so application rejection remains semantic while
invocation or mapping failure remains infrastructure. See the
[adapter SDK guide](TARGET_ADAPTER_SDK.md) or implement the language-neutral
[external target protocol](TARGET_PROTOCOL.md) directly.

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
whose two callables have stable import paths, Parity re-observes the witness in newly started target
processes. A stateful result that disappears in clean execution is reported as an error, never
accepted as a semantic finding. Non-importable live callables are repeated only in the caller's
process, must be pure functions of their arguments, and produce evidence that cannot be replayed
automatically. Project-relative interpreter and import paths can be preserved. Absolute or external
interpreter and import paths are deliberately omitted rather than replaced with the current
environment, so those artifacts remain inspectable evidence but require an explicit configuration
to re-run.

A configured case uses one persistent reference target session and one persistent candidate session for the
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
- `minimum`/`maximum` bound numerical and date/datetime ranges.
- `regex`, `min_length` and `max_length` define reviewable string domains.
- `timezone` uses an IANA zone and adds applicable DST transition boundaries.
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

`PASSED` means every observed reference/candidate outcome satisfied the selected policy. `FAILED`
means Parity found a semantic mismatch or an enforced performance regression. `ERROR` means the
campaign itself could not produce reliable comparison evidence, for example because of invalid
configuration, transport/import/canonicalisation failure, process timeout/crash or corrupt artifact.

When both implementations raise exceptions, `check_exceptions` controls whether equivalent
failure behaviour is required. An exception match is evidence only for that input; it is not a
successful business result. Parity compares normalized exception type/message semantics rather
than raw text: paths, addresses, timestamps, random IDs, dependency versions and witness literals
are ignored, while stable API subjects and structured validation reason codes remain distinct.
FAILED exception findings report data-safe reference/candidate outcomes, well-known qualified
exception types, and allow-listed Pydantic error codes, location shapes and NumPy API tokens when
available. Custom identifier-shaped metadata remains opaque. Raw exception messages and witness
values are never copied into reports; terminal output includes the complete `ms3:` replay
signature.
Target-raised exceptions are semantic outcomes and therefore produce `FAILED`, not `ERROR`, when
they differ. Input mutation is checked separately because two equal return values can leave
materially different downstream state.

## Useful commands

```text
parity init [PATH] [--force]                  create a runnable starter
  --reference TARGET --candidate TARGET       scaffold an existing project pair
  --fixture FILE [--case-name NAME]
  [--reference-adapter NAME] [--candidate-adapter NAME]
  [--reference-python PATH] [--candidate-python PATH]
  [--record-distribution NAME] [--row-key COLUMN]
parity adapter init PATH [--force]            scaffold a Python command adapter
parity adapter serve PATH                     run an SDK adapter (controller-launched)
parity inspect FIXTURE [--output PATH]        infer a portable schema
parity check [--config PATH] [--case NAME]    run campaigns
             [--tag TAG] [--max-examples N]
             [--max-findings N]
             [--stability-repeats N]
             [--jobs N] [--native-threads N]
             [--performance|--no-performance]
             [--json PATH] [--junit PATH] [--markdown PATH]
parity migration check --manifest PATH        gate the declared migration inventory
                       --config PATH [--json PATH]
parity migration init (--reference-package PACKAGE==VERSION | --reference-path PATH)
                      [--candidate-package PACKAGE==VERSION | --candidate-path PATH]
                      [--scaffold] [--json]
                      [--target TARGET | --reference-target TARGET
                                       --candidate-target TARGET]
                      [--fixture FILE]         scaffold or declare a managed workspace
                      [--lane NAME[=REQUIREMENTS]]
parity migration validate [--workspace PATH] [--json]
                                              preflight authored contracts only
parity migration advance --reference-package PACKAGE==VERSION
                                              move the active adjacent pair
parity migration setup [--workspace PATH]     prepare locked target environments
                       [--refresh-locks]
parity migration run [--workspace PATH]       prepare and run every dependency lane
                     [--refresh-locks] [--json]
parity evidence verify REPORT                 replay report-referenced findings
                       [--artifact-root PATH] [--json PATH]
parity replay ARTIFACT [--json]               reproduce a counterexample from any cwd
parity schema list                            list published machine contracts
parity schema NAME [--output PATH]            emit one versioned JSON Schema
parity doctor [--json]                        report runtime readiness
parity doctor --config PATH [--case NAME]     preflight configured targets
              [--json]
parity --version | parity version             print the installed version
```

## Current boundaries

Parity currently supports canonical Arrow/frame inputs, Python callables through built-in adapters,
and arbitrary local executables through the target protocol. Canonical returns, raises, input
mutation and process performance are first-class observations. A reviewed adapter can project a
CLI, file or database result into that return contract, but Parity does not yet isolate and compare
side effects such as filesystem changes, transactions, network calls or subprocesses itself.
Target processes isolate failures; they do not securely sandbox hostile code. See the
[command-adapter SDK](TARGET_ADAPTER_SDK.md), [use cases and boundaries](USE_CASES.md), the
[roadmap](ROADMAP.md), [architecture](ARCHITECTURE.md) and [threat model](THREAT_MODEL.md).
