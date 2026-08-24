# Configuration reference

Parity configuration is strict TOML. Unknown fields are errors so a misspelt policy cannot silently
fall back to a default. Paths are resolved relative to the configuration file.

## Top level

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `version` | integer | `2` | Configuration format; only version 2 is accepted. Version 1 fields are rejected. |
| `artifact_dir` | path | `.parity` | Root for counterexamples and reports. |
| `fail_fast` | boolean | `false` | Stop the suite after the first failed/error case. |
| `jobs` | integer | `1` | Independent cases to run concurrently, 1 through 256. Values above 1 are incompatible with `fail_fast`. |
| `native_threads` | integer/null | none | Opt-in default for common BLAS/OpenMP thread pools in each worker, 1 through 256. |
| `compatibility_budget` | relative path/null | none | Contained version 1 budget of reviewed case/finding approvals. |
| `cases` | array of tables | conditional | One or more uniquely named campaigns, mutually exclusive with `cases_file`. |
| `cases_file` | relative path | conditional | Contained file holding the campaigns, mutually exclusive with inline `cases`. |
| `case_defaults` | table | none | Bounded defaults expanded into every declared case. |

Exactly one of inline `[[cases]]` or `cases_file` is required.

## Reusable cases and bounded defaults

A large migration can keep its root policy separate from reusable case declarations:

```toml
# parity.toml
version = 2
artifact_dir = ".parity"
cases_file = "migrations/cases.toml"

[case_defaults.reference]
workdir = "."
required_distributions = { pandas = ">=2,<3" }

[case_defaults.candidate]
workdir = "."
required_distributions = { polars = ">=1,<2" }

[case_defaults.generation]
max_examples = 500
stability_repeats = 2
```

The referenced file is deliberately small and non-recursive:

```toml
# migrations/cases.toml
version = 2

[[cases]]
name = "orders"

[[cases.invocation.args]]
kind = "frame"
fixture = "tests/fixtures/orders.parquet"

[cases.reference]
target = "migration_wrappers:orders_reference"
adapter = "pandas"

[cases.candidate]
target = "migration_wrappers:orders_candidate"
adapter = "polars"
```

The cases file must be a relative path that resolves inside the root configuration directory. It
contains only `version = 2` and a non-empty `[[cases]]` array; it cannot include another
`cases_file`, top-level policy or defaults. All fixture, interpreter and workdir paths in expanded
cases still resolve relative to the root `parity.toml`, not the cases file.

`[case_defaults]` accepts only:

- partial `reference` and `candidate` tables without `target` or `command`;
- partial `comparison`, `generation` without `generator`, and `performance` tables;
- `timeout_seconds`.

Case identity, targets/commands, the complete invocation or custom generator, and tags must remain
visible on each case. Case fields override defaults.
Nested tables merge recursively; scalars and lists replace the inherited value. Parity validates
and fingerprints the fully expanded configuration, so an extracted/defaulted config has the same
effective model and hash as equivalent inline declarations.

## Compatibility budget

Create a review ledger from a current suite or migration report, approve exact findings with a
rationale, then reference it from `parity.toml`:

```bash
parity budget init .parity/report.json compatibility.toml
parity budget approve compatibility.toml orders ms3:... \
  --reason "Reviewed intentional change"
```

```toml
version = 2
compatibility_budget = "compatibility.toml"
```

The version 1 budget contains `source_report_sha256` and one or more `[[findings]]` entries:

| Key | Type | Meaning |
|---|---:|---|
| `case` | string | Exact configured case name. |
| `finding_signature` | string | Exact `ms3:` behavioural-class signature from the captured report. |
| `decision` | `review` / `approved` / `rejected` | Only `approved` permits the finding. |
| `reason` | string/null | Required non-blank rationale for approved or rejected decisions. |

Approved findings remain in every report but do not block the case. Review/rejected entries and
all new signatures still fail. Fixed approvals are reported as no longer observed. The budget file
must resolve inside the configuration directory and its validated content participates in the
effective-configuration hash. For each case, `generation.max_findings` must exceed the number of
approvals so discovery always has capacity for at least one new difference class. See
[Compatibility budgets and reference retirement](COMPATIBILITY_BUDGETS.md).

## Migration manifest

`parity migration check` loads a separate strict TOML manifest. This inventory is not part of
`parity.toml` and does not change its version 2 configuration contract. The default paths are
`migration.toml` and `parity.toml`; pass `--manifest` and `--config` explicitly when they differ.

```toml
version = 1

[[units]]
id = "orders-transform"
cases = ["orders-control", "orders-null-keys"]

[[units]]
id = "plot-orders"
excluded_reason = "Presentation output is outside this migration."

[[units]]
id = "customer-summary"
```

Top-level fields are:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `version` | integer | `1` | Migration-manifest format; only version 1 is accepted. |
| `units` | array of tables | required | One or more uniquely identified declared migration units. |

Each `[[units]]` accepts:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `id` | string | required | Unique `[A-Za-z0-9_.-]+` inventory identifier. |
| `cases` | string array | `[]` | Unique case names from the selected `parity.toml`. |
| `excluded_reason` | string | none | Non-whitespace reason that this unit is outside scope. |

`cases` and `excluded_reason` are mutually exclusive. Omitting both is valid and derives an
`uncovered` unit so work can remain visible without making the manifest invalid. Unknown mapped
case names are configuration errors detected before any project callable executes. A case may
support several units and is executed only once. Cases that occur in `parity.toml` but nowhere in
the manifest are not selected by the migration command.

The migration command attempts the complete mapped-case union even when `fail_fast = true`. It has
no case, tag, generation-budget or performance override, because a partial or weakened execution
cannot certify the inventory:

```bash
parity migration check \
  --manifest migrations/migration.toml \
  --config migrations/parity.toml \
  --json migrations/.parity/migration-status.json
```

Derived unit statuses are `passed`, `failed`, `error`, `excluded` and `uncovered`. A mapped case
that errors, is skipped, produces zero observations or is unexpectedly absent makes its unit an
error. A unit passes only when every mapped case passes with execution evidence. Error takes
precedence over failure.

The overall result passes only when at least one unit passed and every remaining unit passed or was
explicitly excluded. A failed or uncovered unit, and an all-excluded manifest, return exit `1`.
Invalid configuration and error/incomplete execution evidence return exit `2`.

`--json` writes migration report schema version 1 with these top-level fields:

| Key | Meaning |
|---|---|
| `schema_version` | Migration report format, currently `1`. |
| `status` | Overall `passed`, `failed` or `error` result. |
| `summary` | Derived `total`, `passed`, `failed`, `error`, `excluded` and `uncovered` counts. |
| `units` | Unit ID/status, redacted exclusion reason and mapped case name/status/example count. |
| `manifest_sha256` | Canonical fingerprint of the effective migration inventory. |
| `parity` | Data-safe Parity report schema version 4 for the mapped-case union. |

The nested Parity report's provenance contains its effective `config_sha256`. Unit IDs, case names
and exclusion reasons pass through report redaction. Reports omit compared invocation and output
values, but counterexample artifacts referenced by the nested report may contain fixture-derived or
generated inputs.

`migration init`, `migration validate`, `migration run` and `replay` use boolean `--json` for an
agent-result schema v1 document on stdout; unlike `migration check --json PATH`, no output path
follows the flag. The envelope contains bounded status/check/issue records, report and artifact
references, argv-array next commands and only data-safe report projections. Exit status remains
`0`, `1` or `2`.

Run `parity schema list` to enumerate public contracts and `parity schema NAME` to emit a
self-describing Draft 2020-12 schema. Published names include `config`, `workspace`,
`migration-manifest`, `compatibility-budget`, `distilled-contract`, `suite-report`, `finding`,
`migration-report`, `replay`, `artifact-manifest`, `checklist` and `agent-result`. These are frozen
package resources: a contract version's schema bytes do not vary with the installed Pydantic
release.

This manifest is a reviewed declaration, not an API-discovery mechanism. Parity cannot detect a
public API omitted from `units` or prove that a mapped case exercises the behaviour its unit ID
claims. Split partially excluded behaviour into separate units and review the inventory, wrappers
and exclusions before relying on the gate.

## Migration workspace

Install the optional environment support, then declare each side as either one exact released
requirement or an existing checkout. When `migrations/parity.toml` does not exist, the same command
can scaffold its first fixture-backed case and the migration ledger:

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

```bash
parity migration init \
  --reference-path ../main-worktree \
  --candidate-path ../feature-worktree
parity migration run
```

When the config is absent, choose one of two creation modes:

- `--scaffold` creates a deliberately incomplete Arrow wrapper, tiny JSON fixture, minimal config
  and explicit review checklist. It cannot be combined with target, fixture, adapter, distribution
  or row-key options.
- Explicit mode requires `--fixture` plus either `--target` or both side-specific targets.
  `--target` supplies both sides; `--reference-target` and `--candidate-target` may override it.
  This mode may also set `--case-name`, side adapters, repeatable recorded distributions and
  repeatable row keys. It creates no wrapper module, so its targets must already exist.

Both modes derive the starter migration ledger from the resulting config. These files are
reviewable contract inputs, not API discovery. If `parity.toml` already exists, all case-creation
options are rejected: omit them to load the reviewed contract. `--force` applies only to
`parity.workspace.toml` and never replaces `parity.toml` or scaffold files. In explicit mode,
`--fixture` is interpreted from the invocation directory and serialized relative to the generated
config. Managed wrappers are imported from the workspace directory. Initialization validates
import-target spelling and fixture readability; `migration run` preflights target imports after
environment setup. `migration validate` also rejects an unresolved generated checklist or the
untouched scaffold adapter without creating environments.

By default `migration init` writes a strict workspace v3 at
`migrations/parity.workspace.toml`, uses the current
checkout as the candidate when neither candidate flag is present, and creates
`migrations/migration.toml` when that ledger is absent. The starter maps every configured case to
one `core-regression` unit and must be reviewed as an inventory. Workspace format 3 is a breaking
schema; its source mapping is symmetric:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `version` | integer | `3` | Workspace format; only version 3 is accepted. |
| `reference_package` | exact requirement | conditional | Released reference; CLI `--reference-package`. |
| `reference_path` | path | conditional | Local reference; CLI `--reference-path`. Exactly one reference source field is required. |
| `candidate_package` | exact requirement | conditional | Released candidate; CLI `--candidate-package`. |
| `candidate_path` | path | conditional | Local candidate; CLI `--candidate-path`. Exactly one candidate source field is required; the CLI writes `.` here when neither candidate flag is supplied. |
| `python` | `major.minor` | invoking Python | Shared target Python shorthand, at least 3.8. |
| `reference_python` | `major.minor` | `python` | Reference target Python override, at least 3.8. |
| `candidate_python` | `major.minor` | `python` | Candidate target Python override, at least 3.8. |
| `config` | path | `parity.toml` | Parity case configuration. |
| `manifest` | path | `migration.toml` | Migration inventory. |
| `checklist` | path/null | none | Optional agent-review checklist; unresolved items make validation non-passing. |
| `report_dir` | contained path | `.parity/workspace/reports` | Per-lane migration JSON reports. |
| `lanes` | array of tables | one `default` lane | Unique dependency lanes. |

Each `[[lanes]]` has a required `[A-Za-z0-9_.-]+` `name` and an optional `requirements` path.
Equivalent CLI values are repeatable `--lane NAME` or `--lane NAME=REQUIREMENTS`. Workspace source,
config, manifest and lane paths supplied to `migration init` are interpreted from the invocation
directory and serialized relative to the workspace file. Paths inside the saved TOML are resolved
beside that file. Fixture rebasing follows the separate config rule above. `report_dir` must remain
inside the workspace directory.

Both package fields accept exactly one unconditional, non-wildcard PEP 508 `==` requirement, such
as `package==1.2.3` or `package[extra]==1.2.3`. Reference and candidate must normalize to the same
distribution name. The four valid source combinations are released/released, released/local,
local/released and local/local. No other managed source keys are accepted.

Use `python` when both sides support the same target interpreter. For a runtime migration, pass
`--reference-python 3.8 --candidate-python 3.12` (or set both side-specific fields); either side may
override a shared `python`. These are target runtimes, not the Parity controller: the controller
still requires Python 3.11 or newer.

`parity migration setup` resolves separate hash-pinned requirements locks for the reference and
candidate in every lane, then prepares an isolated worker pair. `parity migration run` performs
that setup and runs the complete manifest in every lane. Managed execution uses the workspace
directory as both worker working directories, supplies the two prepared interpreter paths,
binds each released side to its exact workspace version and records the subject distribution on
both sides. An explicit `required_distributions` constraint that excludes either exact version is
rejected. It writes
`<report_dir>/<lane>.json`. The active lane report is removed before execution, so an interrupted
run cannot leave an earlier green report looking current. Locks keep dependency selection
stable on later runs; `--refresh-locks` deliberately asks the resolver to upgrade them. The command
returns `2` if any lane errors, otherwise `1` if any lane fails, otherwise `0`.

Configured replay paths are based on the directory containing `parity.toml`. Replay v3 records that
base as a bounded ancestor of the artifact and resolves it from the artifact itself, never from the
process current directory. The managed workspace
directory and its private environments must be contained by that configuration directory; a config
in a child or unrelated directory is rejected before setup. Keeping `parity.toml` beside
`parity.workspace.toml` is the default and simplest layout.

Every local side is installed editable only in its own worker and, during `migration run`, Parity
proves that installed distribution metadata and importable modules resolve to the declared source
in every effective worker environment. Its target runtime reports a path-free Git HEAD, dirty flag
and source digest; findings and replay bind that identity. Each local source must therefore be a Git
worktree with a committed HEAD, although a dirty worktree is allowed and explicitly bound.

Local/local mode additionally captures paired driver snapshots before and throughout the run.
Semantic lane evidence is written only when both target identities match those snapshots; any
source change invalidates all active reports. Success writes
`<report_dir>/source-provenance.json`; this report intentionally contains no paths, branch names,
file names or source values and remains meaningful if the report directory is relocated. Mixed
local/released runs retain their one local target identity in runtime/finding/replay evidence but do
not create this paired report or receive the continuous two-worktree driver checks.

`parity migration advance --reference-package package==version` atomically changes only an active
exact reference. The distribution name must be unchanged. Candidate, Python, paths and lanes are
preserved; current lane reports are invalidated. For a released candidate, update
`candidate_package` separately for the next pair before running it; `advance` never guesses the next
candidate release. Parity intentionally stores no A→…→M history.
Keep durable cases mapped in a permanent manifest unit and replace transition-specific units for
each adjacent pair. `advance` does not move a local branch or worktree; regenerate or edit a
local/local declaration (using `migration init --force` when replacing the workspace file) to
choose the next reviewed `reference_path`.

tox, tox-uv and uv implement this lifecycle behind the Parity commands. Generated locks,
environments, tox configuration and the default uv cache are private state under
`.parity/workspace`; users do not need to author tox configuration. When `UV_CACHE_DIR` is unset,
Parity uses `.parity/workspace/cache` so an unavailable user cache does not block setup. An explicit
`UV_CACHE_DIR` is preserved for shared or offline caches. The `.parity` root is self-ignoring even
when a consumer repository has no root `.gitignore`. Parity never clones a repository, selects or
changes its revision, applies patches or edits either local source. Local packaging metadata and
resolved dependencies are executable supply-chain inputs and must be trusted. Managed target
environments contain the target package and the PyArrow transport; package metadata or lane
requirements must supply pandas, Polars or any other selected adapter dependency. They do not
install the Parity controller. Use explicit endpoint `python` paths in `parity.toml` when
environments are provisioned elsewhere.

Managed setup requires each local distribution name to match the shared subject name and to be
declared statically as `project.name`, `tool.poetry.name`, or `setup.cfg` `[metadata] name`. It
rejects dynamic `setup.py`-only names because executing project code merely to discover identity
would make validation unsafe and a stale or differently named editable could cause a false pass.
For projects with dynamic distribution metadata, skip the managed workspace and provision both
worker interpreters explicitly in `parity.toml`.

The workspace directory must not expose either local checkout and neither worker's configured or
inherited `PYTHONPATH` may expose a managed source root. Otherwise a flat-layout package could shadow
the intended editable while distribution metadata still looked correct. Keep workspaces and wrapper
modules in a neutral `migrations/` directory; editable installations provide local imports without
adding source roots manually. The workspace directory must also remain inside the configured
`parity.toml` directory when automatic replay is required.

## Retained evidence verification

`parity evidence verify REPORT [--artifact-root PATH] [--json PATH]` accepts a suite JSON report
(schema 4) or migration JSON report (schema 1 with its nested suite). Every failure entry must name
a replayable artifact and carry an `ms3:...` mismatch signature. Duplicate report entries are
verified once in report order.

Without `--artifact-root`, a report located anywhere beneath the artifact directory named in its
entries resolves that ancestor automatically; otherwise Parity resolves the named directory below
the current directory. A supplied root relocates that same named directory. Report paths must remain
contained, regular manifest-bound artifacts. Verification checks stored hashes and result metadata,
requires verified runtime provenance, and replays the exact saved input.

Configured artifact contracts make interpreter, workdir and path-like executable paths relative to
the directory containing the loaded `parity.toml`. Replay v3 locates that base from the artifact,
so the command may be launched from any directory. Those paths must remain configuration-local;
configuration-local virtual-environment entry points may still resolve through
their final symlink to a host Python. A non-importable live callable, external
interpreter/workdir/executable, missing configuration-local executable, external artifact root or
redacted JSON invocation leaves the artifact inspectable but non-replayable. An optional retained
`replay_blockers` map records only bounded reason codes: side-specific `live_callable`,
`external_python`, `external_workdir`, `external_command` or `missing_command`, and artifact-level
`external_artifact_root` or `redacted_invocation`. Replay reports an actionable remedy without
persisting the external host path or secret value.

Exit codes are:

- `0`: every artifact reproduced exactly its expected mismatch signature (`verified`);
- `1`: at least one valid artifact now passes or produces a different mismatch signature (`stale`);
  and
- `2`: report/artifact integrity, provenance or execution could not be established (`error`).

Errors take precedence over stale results. `--json` writes data-safe evidence report schema 1 with
the source report hash, aggregate counts, artifact statuses and a bounded `reason_code` for every
non-verified entry. Reason codes distinguish an unavailable/invalid artifact, signature or case
mismatch, unverified provenance, replay failure/error, a finding that disappeared, and a finding
whose mismatch shape changed; they never include caught exception text or absolute paths. The
report does not copy counterexample values. This is behavioural and local-integrity verification,
not trust establishment. `ms3:` is a
data-free mismatch-shape digest, not a digital signature, and replay executes arbitrary configured
Python. Review the report, artifact and checkout before running it.

## Case

Declare cases with `[[cases]]`.

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `name` | string | required | Unique `[A-Za-z0-9_.-]+` case identifier. |
| `reference` | table | required | Baseline implementation. |
| `candidate` | table | required | Replacement implementation. |
| `invocation` | table | conditional | Complete shared `callable(*args, **kwargs)` contract. Mutually exclusive with `generation.generator`. |
| `comparison` | table | defaults below | Equivalence policy. |
| `generation` | table | defaults below | Search limits and determinism. |
| `performance` | table | defaults below | Benchmark and regression policy. |
| `timeout_seconds` | float | `30` | Per invocation timeout, greater than 0 and at most 3600. |
| `tags` | string array | `[]` | Selection labels used by `parity check --tag`. |

Every case requires exactly one of `invocation` or `generation.generator`. The reference and
candidate receive the same normalized positional values, keyword names and keyword values. When
their APIs differ, use small side-specific wrappers; endpoint-specific arguments are deliberately
not part of configuration v2.

## Invocation

`[cases.invocation]` describes the complete call. It may be empty for a zero-argument callable.
Repeat `[[cases.invocation.args]]` for positional arguments in call order, use
`[cases.invocation.kwargs.<name>]` for named arguments, and optionally use
`[cases.invocation.varargs]` for a generated frame sequence expanded as `*args`.

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `args` | argument array | `[]` | Fixed positional call slots in declaration order. |
| `kwargs` | named argument tables | `{}` | Fixed keyword call slots. |
| `varargs` | `frames` argument/null | none | Homogeneous frame sequence expanded after fixed positional slots. |
| `relationships` | relationship array | `[]` | Joint constraints between named individual `frame` arguments. |

Each argument has one `kind`. A `frame` accepts:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `kind` | `frame` | required | Argument discriminator. |
| `fixture` | path/null | none | Parquet, Arrow IPC, CSV or JSON seed frame. |
| `schema` | table/null | inferred from fixture | Generated frame domain. A fixture, schema or both is required. |
| `name` | string/null | derived | Stable relationship identity. |
| `generate` | boolean | `true` | Generate and shrink beyond the fixture; `false` requires a fixture. |

A `json` argument accepts:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `kind` | `json` | required | Argument discriminator. |
| `values` | JSON-like array | required | Non-empty finite choices for this call slot. |

A `frames` argument accepts:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `kind` | `frames` | required | Argument discriminator. |
| `fixtures` | path array | `[]` | Ordered seed frames for one deterministic sequence. |
| `schema` | table/null | inferred from first fixture | Shared schema for every item. |
| `name` | string/null | derived | Stable diagnostic identity; sequences cannot join relationships. |
| `min_items` | integer | `0` | Minimum sequence length, 0 through 256. |
| `max_items` | integer | `8` | Maximum sequence length, `min_items` through 256. |
| `container` | `list` / `tuple` | `list` | Container passed to one call slot; varargs require `tuple`. |
| `generate` | boolean | `true` | Generate item frames and lengths; `false` uses exactly `fixtures`. |

Configured fixture counts must satisfy `min_items`/`max_items`. A fixed empty sequence
(`generate = false`, `min_items = 0`, `max_items = 0`) needs no item schema.

There may be up to 256 positional and 256 keyword call slots. A frame sequence may contain up to
256 frames; expanded varargs and other positional slots together may not exceed 256. Keyword names
are non-keyword Python identifiers of at most 128 characters. One JSON argument is limited to
256 KiB and all JSON arguments in one invocation to 512 KiB, keeping in-process, isolated and
command execution on the same bounded contract.

A single-frame case is one positional argument:

```toml
[[cases.invocation.args]]
kind = "frame"
fixture = "tests/fixtures/orders.arrow"
```

A join can use any number of positional or keyword frames within those bounds. `name` gives a frame
a stable identity for relationships; otherwise a keyword name or positional `arg0`, `arg1`, …
identity is used.

```toml
[[cases.invocation.args]]
kind = "frame"
name = "orders"
fixture = "tests/fixtures/orders.arrow"

[cases.invocation.kwargs.customers]
kind = "frame"
name = "customers"

[cases.invocation.kwargs.customers.schema]
min_rows = 1
max_rows = 20

[[cases.invocation.kwargs.customers.schema.columns]]
name = "customer_id"
dtype = "int64"
nullable = false
unique = true

[[cases.invocation.relationships]]
kind = "foreign_key"
child = { input = "orders", columns = ["customer_id"] }
parent = { input = "customers", columns = ["customer_id"] }
allow_nulls = true
```

For a reduce-style API that accepts one sequence, use a `frames` argument:

```toml
[cases.invocation.kwargs.batches]
kind = "frames"
min_items = 1
max_items = 32
container = "list"

[cases.invocation.kwargs.batches.schema]
min_rows = 0
max_rows = 100

[[cases.invocation.kwargs.batches.schema.columns]]
name = "value"
dtype = "float64"
```

For `def reduce(*frames): ...`, declare the same contract under
`[cases.invocation.varargs]` with `container = "tuple"`; Parity expands the generated sequence
instead of passing a container. JSON arguments can appear anywhere and make call modes explicit:

```toml
[[cases.invocation.args]]
kind = "json"
values = ["sum", "mean"]

[cases.invocation.kwargs.skip_nulls]
kind = "json"
values = [true, false]
```

`frames` sequences are homogeneous and cannot participate in cross-frame relationships. Use
individual named `frame` arguments or a custom full-invocation generator for heterogeneous or
dependent variable-arity calls.

### Relationships

`[[cases.invocation.relationships]]` jointly generates and shrinks constraints between named
individual frame arguments:

| `kind` | Fields | Meaning |
|---|---|---|
| `key_overlap` | `left`, `right`, `min_shared` | Require at least that many shared, distinct non-null keys. |
| `foreign_key` | `child`, `parent`, `allow_nulls` | Every non-null child key occurs in the parent key domain. |
| `equal_row_count` | `inputs` | Selected inputs have equal row counts. |
| `cardinality` | `left`, `right`, `relationship` | Enforce `one_to_one`, `one_to_many`, `many_to_one` or `many_to_many` key uniqueness. |

Keys are `{ input = "name", columns = ["one", "or_more"] }`. Paired keys must have the same
arity and compatible portable dtype families. Cardinality does not imply overlap or inclusion; add
the corresponding relationship explicitly. `one_to_many` makes the left key unique,
`many_to_one` makes the right key unique, `one_to_one` makes both unique and `many_to_many` adds no
uniqueness constraint. A foreign key with `allow_nulls = false` rejects null child keys; when true,
only non-null child keys must occur in the parent. A fixture-only frame has its schema inferred
before relationship validation. Every referenced frame must use `generate = true`. Relationship
fixtures are atomic: either every referenced frame has one or none does. Unrelated invocation
arguments keep their own independent generation.

## Callable specification

Both `[cases.reference]` and `[cases.candidate]` accept:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `target` | string/null | conditional | Python import target `package.module:callable.path`; each dotted component must be a Python identifier. Exactly one of `target` or `command` is required. |
| `command` | string array/null | conditional | Protocol-speaking executable argument vector. Exactly one of `target` or `command` is required. |
| `canonicalizer` | string/null | none | Python import target applied to a successful raw return before Arrow/JSON canonicalisation. |
| `adapter` | enum | `auto` | `auto`, `pandas`, `polars` or `arrow`. Auto infers annotated dataframe types and otherwise uses the core Arrow fallback, including for JSON-only calls. |
| `pandas_input` | `arrow` / `native` | `arrow` | Pandas input materialization; ignored when the resolved adapter is not pandas. |
| `python` | path | current Python | Interpreter for isolated execution. |
| `workdir` | path | config directory | Working directory and import root. |
| `environment` | string table | `{}` | Literal environment overrides for the worker. |
| `record_distributions` | string array | `[]` | Additional distribution versions to record inside this worker. |
| `required_distributions` | string table | `{}` | Distribution names mapped to required PEP 440 version specifiers. |
| `native_threads` | integer/null | none | Endpoint override for the common BLAS/OpenMP thread-pool limit. |

`target`, `canonicalizer`, `adapter`, `pandas_input` and `python` apply only to Python endpoints. A
command endpoint owns input adaptation, application invocation and output canonicalisation through
[target protocol v2](TARGET_PROTOCOL.md); it cannot set those Python-only fields. `workdir`,
`environment`, distribution provenance and `native_threads` apply to both endpoint kinds.
Unannotated dataframe callables should select `pandas`, `polars` or `arrow` explicitly; the
dependency-light `auto` fallback does not assume pandas is installed.

Paths may be relative. A configured `python` path is anchored to the configuration directory
without dereferencing its final virtual-environment symlink; two venv entry points that share one
base Python therefore remain distinct execution environments. Other project paths retain their
normal resolved semantics. A relative command executable such as `./bin/adapter` is launched from
the endpoint `workdir`, which defaults to the configuration directory. Commands are argument
vectors, never shell strings: they contain 1 through 64 non-empty arguments, each at most 4,096
characters and without NUL or newline characters. Do not commit secrets in `environment` or
command arguments.

The controller report records its own runtime provenance. Portable Python targets independently
report Python/platform identity plus NumPy, pandas, Polars and PyArrow when installed; they report
`parity_version = null` because Parity is not installed in the target. Command targets report the
generic runtime identity required by target protocol v2. `record_distributions` adds up to 64
explicitly named distributions, using distribution names rather than import names (for example
`scikit-learn`, not `sklearn`). Names are normalized and duplicates are errors. This field only
reads target-reported or installed metadata; it never installs or imports the named distribution.

`required_distributions` turns selected metadata into a fail-closed runtime contract:

```toml
[cases.reference]
target = "migration_wrappers:reference"
required_distributions = { your-library = "==1.2.*", pandas = ">=2,<3" }

[cases.candidate]
target = "migration_wrappers:candidate"
required_distributions = { your-library = ">=2,<3", polars = ">=1,<2" }
```

Names use the same normalization and shared limit as `record_distributions`. Specifiers follow PEP
440, except arbitrary `===` equality is rejected; an empty specifier means any valid installed
version. Required names are recorded automatically. Before target import or invocation, Parity
returns a data-safe `RuntimeContractError` when metadata is missing/unavailable or the installed
version is outside its range. There is no target-side Parity-version requirement. The
`parity doctor --config` command shows each requested requirement and satisfaction state, and
checks both transports before importing either endpoint. If one transport fails, the deferred peer
endpoint has status `not_checked` and error code `TargetEndpointNotChecked`. The command never
invokes the behavioural target.
Protocol commands must report every required distribution in their runtime response; leaving a
required name absent fails closed.

`pandas_input = "arrow"` converts the canonical input to pandas with Arrow extension dtypes. It
preserves distinctions such as nullable integers and Arrow null versus a valid IEEE NaN, and is the
stable default. `pandas_input = "native"` uses PyArrow's default pandas conversion for callables
that expect conventional NumPy/object dtypes. Native conversion is pandas-version-dependent and
can widen nullable integers or collapse null and NaN into the same missing value. This setting
changes only the callable's input; returned pandas objects still use Parity's normal Arrow
canonicalization.

## Frame schema

Every `frame` or `frames` argument's nested `[...schema]` table accepts:

| Key | Type | Default | Constraint |
|---|---:|---:|---|
| `min_rows` | integer | `0` | At least 0. |
| `max_rows` | integer | `30` | From `min_rows` through 10,000. |
| `unique_together` | array of string arrays | `[]` | Each named tuple must be unique. |
| `constraints` | array of tables | `[]` | Frame-local valid-domain constraints described below. |
| `columns` | array of tables | required | One or more unique columns. |

Declare columns by repeating the argument's `[[...schema.columns]]` table:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `name` | string | required | Non-empty unique column name. |
| `dtype` | string | required | Portable family or concrete common dtype. |
| `nullable` | boolean | `true` | Permit null generation. |
| `unique` | boolean | `false` | Require distinct values in this column. |
| `minimum` | JSON-like scalar | none | Inclusive lower bound. |
| `maximum` | JSON-like scalar | none | Inclusive upper bound. |
| `categories` | array | none | Non-empty closed value set. |
| `examples` | array | `[]` | Values deliberately included in deterministic inputs. |
| `regex` | string | none | Full-match Python regular expression for `string` and `category` values. |
| `min_length` | integer | none | Inclusive minimum text length for `string` and `category` values. |
| `max_length` | integer | none | Inclusive maximum text length for `string` and `category` values. |
| `timezone` | string | none | Valid IANA zone for datetime values; generation uses that zone and probes applicable DST gaps, folds and transition boundaries. |

Portable dtype families are `integer`, `float`, `decimal`, `boolean`, `string`, `category`,
`binary`, `date`, `datetime`, `time`, `duration`, `list`, `struct` and `object`. Common concrete
integer/float widths such as `int8`, `int64`, `uint32`, `float32` and `float64` are retained when
materialising Arrow inputs. Bounds, categories and examples must satisfy the complete column
domain, including text constraints. Write date and datetime values as quoted ISO strings in TOML;
native TOML date/datetime literals are not JSON-like configuration values. A datetime without an
explicit offset is interpreted as local wall time when `timezone` is set; an offset-aware datetime
is converted to the configured zone.

```toml
[[cases.invocation.args.schema.columns]]
name = "trade_time"
dtype = "datetime"
nullable = false
minimum = "2024-01-01T00:00:00"
maximum = "2024-12-31T23:59:59"
timezone = "America/New_York"

[[cases.invocation.args.schema.columns]]
name = "symbol"
dtype = "string"
nullable = false
regex = "[A-Z][A-Z0-9]{0,7}"
min_length = 1
max_length = 8
```

### Frame constraints

Declare each constraint under its argument schema, for example
`[[cases.invocation.args.schema.constraints]]` or
`[[cases.invocation.kwargs.left.schema.constraints]]`.

| `kind` | Fields | Meaning |
|---|---|---|
| `sorted_by` | `columns`, `descending=false`, `nulls="last"` | Generate the complete frame in lexicographic order by one or more columns. `nulls` is `first` or `last`. |
| `row_comparison` | `left`, `operator`, `right` | Require the named columns to satisfy `lt`, `le`, `eq`, `ge` or `gt` on every row where both values are non-null. |

```toml
[[cases.invocation.args.schema.constraints]]
kind = "sorted_by"
columns = ["account_id", "event_time"]
descending = false
nulls = "last"

[[cases.invocation.args.schema.constraints]]
kind = "row_comparison"
left = "start_time"
operator = "le"
right = "event_time"
```

At most one `sorted_by` constraint is accepted per frame. Referenced columns must exist and row
comparisons must use compatible, orderable types. Constraints are preserved by deterministic
cases, generated search, shrinking and invocation-relationship rewrites; impossible domains
are rejected during validation rather than weakened. The initial row-comparison vocabulary accepts
independent column pairs; overlapping comparisons that reuse a column are rejected until Parity can
construct and shrink the complete constraint graph without filter-only search. Explicit fixtures
must satisfy frame constraints even when `adversarial_examples = false`.

## Comparison policy

`[cases.comparison]`:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `column_order` | `strict` / `ignore` | `strict` | Whether output column position is semantic. |
| `row_order` | `strict` / `ignore` / `keyed` | `strict` | Positional, bag-like or key-aligned output rows. |
| `row_keys` | string array | `[]` | Ordered composite output key; non-empty for `keyed`, empty otherwise. |
| `dtype` | `strict` / `compatible` / `ignore` | `compatible` | Concrete, family-level or no dtype check. |
| `names` | `strict` / `case_insensitive` | `strict` | Column-name comparison. |
| `null_equal` | boolean | `true` | Treat nulls at matching positions as equal. |
| `nan_equal` | boolean | `true` | Treat IEEE NaNs at matching positions as equal. |
| `null_nan_equal` | boolean | `false` | Treat a null and an IEEE NaN at matching positions as equal. |
| `signed_zero_equal` | boolean | `true` | Treat `-0.0` and `0.0` as equal. |
| `check_exceptions` | boolean | `true` | Compare returned/raised state and exception contract. |
| `check_input_mutation` | boolean | `true` | Detect changes to callable input. |
| `rtol` | float | `1e-7` | Non-negative relative numeric tolerance. |
| `atol` | float | `0` | Non-negative absolute numeric tolerance. |
| `datetime_tolerance_ns` | integer | `0` | Non-negative temporal tolerance in nanoseconds. |
| `ignored_columns` | string array | `[]` | Columns removed from both outputs before comparison. |
| `overrides` | array of tables | `[]` | Policy patches for selected nested output paths. |

For finite numbers, equivalence follows the configured absolute/relative tolerance. Order-insensitive
comparison preserves multiplicity: two identical rows are not the same as one.

Use keyed comparison when output order is irrelevant but each row has a unique business key:

```toml
[cases.comparison]
row_order = "keyed"
row_keys = ["account_id", "event_id"]
dtype = "compatible"
```

`row_keys` is ordered because it defines a composite identity. Every named key column must be
present on both outputs, must not be ignored, and the composite key must be unique on each side.
Column lookup follows the configured `names` policy; ambiguous names fail closed. Parity aligns
rows by key identity, then applies the ordinary cell policy, including to key cells.
Null, NaN and signed-zero identity follows their explicit equality switches, but numeric and
datetime tolerances do not make two different keys identical. Non-scalar, non-alignable, missing,
unexpected or duplicate keys fail explicitly instead of falling back to order-insensitive greedy
row matching. Any captured values remain in the private counterexample artifact; data-safe reports
identify only the mismatch shape and cell path. When row counts differ, keyed mode reports the more
useful missing or unexpected key evidence instead of a generic shape mismatch.

### Per-output policies

Use `[[cases.comparison.overrides]]` when one returned subtree needs a different dataframe or
numeric policy. `path` is a non-root RFC 6901 JSON Pointer into a returned mapping or sequence.
Escape `~` as `~0` and `/` as `~1`; a complete `*` segment is Parity's one-level wildcard
extension. Overrides inherit the case policy for unspecified fields and apply to the selected
subtree. Matching entries are applied in declaration order, so put a broad wildcard or parent
before a more specific child.

```toml
[cases.comparison]
row_order = "strict"
rtol = 1e-7

[[cases.comparison.overrides]]
path = "/groups/*"
row_order = "keyed"
row_keys = ["id"]

[[cases.comparison.overrides]]
path = "/groups/priority"
rtol = 1e-10
```

An override can set `column_order`, `row_order`, `row_keys`, `dtype`, `names`, the four
null/NaN/signed-zero switches, `rtol`, `atol`, `datetime_tolerance_ns` and `ignored_columns`.
Exception and input-mutation checks apply to the whole invocation and remain case-level policy.

## Generation policy

`[cases.generation]`:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `generator` | import target/null | none | Trusted project factory returning a Hypothesis `SearchStrategy[Invocation]` or a plain iterable of `Invocation` values. |
| `max_examples` | integer | `100` | Property examples, 1 through 100,000. |
| `max_findings` | integer | `10` | Maximum distinct mismatch signatures, 1 through 20. Each additional search receives its own `max_examples` budget. |
| `stability_repeats` | integer | `2` | Total same-input observations per implementation for deterministic passing inputs, 1 through 10. `1` disables the stability check. |
| `search` | boolean | `true` | Run property-based search after deterministic inputs. Set this and `adversarial_examples` to `false` for an exact fixture-only contract. |
| `seed` | integer/null | none | Stable run seed. |
| `deadline_ms` | integer/null | none | Per-Hypothesis-example deadline. |
| `adversarial_examples` | boolean | `true` | Run deterministic edge cases before search. |
| `shrink` | boolean | `true` | Minimise a discovered failing input. |
| `derandomize` | boolean | `false` | Hypothesis deterministic derivation mode. |
| `suppress_too_slow` | boolean | `true` | Suppress Hypothesis health check for slow generation. |

A seed improves repeatability but a saved replay artifact is the strongest reproduction mechanism.
The generator factory is called without arguments in the Parity driver environment, with the
configuration directory on its import path. It returns either a Hypothesis strategy yielding
`parity.Invocation` values, preserving normal generation and shrinking, or an iterable of them,
consumed in stable order and stopped after `max_examples`. Plain iterables do not have a general
shrinking operation.

An `Invocation` holds the complete `args` tuple and `kwargs` mapping. Values may be supported native
frames, portable JSON-like values, or `parity.FrameSequence` for one list/tuple-valued frame
argument. This is the escape hatch for dependent JSON modes, heterogeneous sequences and other
call shapes not expressible by the built-in homogeneous argument strategies:

```python
import pyarrow as pa
from hypothesis import strategies as st
from parity import Invocation


def calls():
    return st.builds(
        lambda window: Invocation(
            args=(pa.table({"value": [1, 2, 3]}),),
            kwargs={"window": window},
        ),
        window=st.integers(min_value=1, max_value=3),
    )
```

A custom generator replaces `[cases.invocation]` and requires `search = true`. Invalid or empty
generator output is an execution error, never a pass. Generator code is trusted project code and
should not contain credentials or depend on either isolated target environment. A failing
invocation is serialized into the recursive replay contract; replay never imports the generator.

With `search = false`, Parity still checks deterministic fixtures or enabled adversarial examples,
including stability repeats, but skips property-based search beyond them. A searchless case without
any deterministic input is rejected instead of passing without evidence. Set
`[cases.performance] enabled = false` separately for a semantic-only fixture check.
Mismatch signatures classify observable difference shapes, not root causes or separate bugs. Every
generated witness is observed twice after shrinking; changing signatures or side-specific output
nondeterminism stops the campaign as an execution error rather than creating questionable evidence.
Separately, deterministic inputs that initially pass are observed `stability_repeats` times. Each
side is compared with its own first observation under strict, zero-tolerance, reflexive canonical
identity. User cross-side ordering/tolerance/null policies cannot label a stable output as unstable,
while matching but equally unstable implementations still cannot produce a false pass.
Semantically changing repeat outcomes, crashes, timeouts or output drift stop the campaign as an
unsigned execution error before generated search or benchmarking. A stable repeated `Raise` is an
ordinary semantic outcome, not an infrastructure failure.
`examples_run` and `deterministic_examples` count each input once; stability observations are
additional callable executions, not additional generated examples.

## Case parallelism

`parity check --jobs N` overrides top-level `jobs`. Parity schedules whole cases only: each case
retains its own deterministic seed, serial Hypothesis search/shrinking, reference/candidate worker
pair and case-named artifact directory. Reports are emitted in declared configuration order rather
than completion order. `fail_fast = true` with more than one job is rejected because in-flight work
cannot provide honest stop-after-first semantics.

Native numerical libraries can create their own thread pools. Parity does not alter them by
default. Set top-level `native_threads = 1`, pass `--native-threads 1`, or set an endpoint's
`native_threads` when case parallelism would otherwise oversubscribe the machine. An explicit
endpoint `environment` value such as `OMP_NUM_THREADS` takes precedence.

## Performance policy

`[cases.performance]`:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `enabled` | boolean | `true` | Benchmark only after semantic success. |
| `warmups` | integer | `1` | Warm-up pairs, 0 through 100. |
| `repeats` | integer | `9` | Measured pairs, 1 through 1,000. Enforced gates require at least 5. |
| `max_slowdown` | float/null | `1.25` | Candidate/reference runtime ratio limit. |
| `max_memory_ratio` | float/null | `1.5` | Candidate/reference peak RSS ratio limit. |
| `min_reference_ms` | float | `1` | Ignore runtime ratio below this reference duration. |
| `fail_on_regression` | boolean | `false` | Convert a policy regression into case failure. |
| `confidence_level` | float | `0.95` | Confidence level for paired bootstrap ratio intervals, greater than 0.5 and less than 1. |
| `bootstrap_samples` | integer | `2000` | Deterministic bootstrap resamples, 100 through 100,000. |

Reference and candidate observations are paired under nearby host load. Parity reports their median
ratio and a deterministic bootstrap confidence interval, and only declares a threshold regression
when the interval's lower bound exceeds the configured limit. Runtime and memory ratios remain
evidence from the current host, not portable guarantees. Use `jobs = 1` for measurements intended
as retained evidence; report-only benchmarks are allowed with concurrent cases but will contend for
the same host. An enforced performance gate already requires `jobs = 1`. When performance is
enabled, an unavailable runner, missing validated passing input or failed benchmark invocation is
infrastructure `ERROR`; Parity never reports a pass after silently omitting requested measurements.

## Complete generated template

Run `parity init`, or call `parity.templates.render_config_template()`, for a versioned and
validated JSON-only example with explicit comparison, generation and performance policies. The
tables above document every available field. To generate one minimal fixture-backed case for
existing code, supply `--reference`, `--candidate` and `--fixture` together or call
`parity.templates.render_project_config()`. The project form omits default-valued tables and does
not create an implementation module.
