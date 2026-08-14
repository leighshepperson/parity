# Configuration reference

Parity configuration is strict TOML. Unknown fields are errors so a misspelt policy cannot silently
fall back to a default. Paths are resolved relative to the configuration file.

## Top level

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `version` | integer | `1` | Configuration format; only version 1 is accepted. |
| `artifact_dir` | path | `.parity` | Root for counterexamples and reports. |
| `fail_fast` | boolean | `false` | Stop the suite after the first failed/error case. |
| `cases` | array of tables | required | One or more uniquely named campaigns. |

## Migration manifest

`parity migration check` loads a separate strict TOML manifest. This inventory is not part of
`parity.toml` and does not change its version 1 configuration contract. The default paths are
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
  --json .parity/migration-status.json
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
| `parity` | Existing data-safe Parity report schema version 3 for the mapped-case union. |

The nested Parity report's provenance contains its effective `config_sha256`. Unit IDs, case names
and exclusion reasons pass through report redaction. No report contains compared dataframe or scalar
values, but counterexample artifacts referenced by the nested report may contain fixture-derived or
generated inputs.

This manifest is a reviewed declaration, not an API-discovery mechanism. Parity cannot detect a
public API omitted from `units` or prove that a mapped case exercises the behaviour its unit ID
claims. Split partially excluded behaviour into separate units and review the inventory, wrappers
and exclusions before relying on the gate.

## Case

Declare cases with `[[cases]]`.

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `name` | string | required | Unique `[A-Za-z0-9_.-]+` case identifier. |
| `reference` | table | required | Existing implementation. |
| `candidate` | table | required | Replacement implementation. |
| `fixture` | path | none | Seed input; Parquet, Arrow IPC, CSV or JSON according to loader support. |
| `schema` | table | none | Generated input contract. At least `fixture` or `schema` is required. |
| `input_bundle` | table | none | Two or three named inputs with optional relational constraints. Mutually exclusive with case-level `fixture`/`schema`. |
| `static_args` | JSON-like array | `[]` | Positional values appended after the input frame. |
| `static_kwargs` | JSON-like table | `{}` | Keyword arguments supplied to both implementations. |
| `comparison` | table | defaults below | Equivalence policy. |
| `generation` | table | defaults below | Search limits and determinism. |
| `performance` | table | defaults below | Benchmark and regression policy. |
| `timeout_seconds` | float | `30` | Per invocation timeout, greater than 0 and at most 3600. |
| `tags` | string array | `[]` | Selection labels used by `parity check --tag`. |

TOML requires case-level scalar keys such as `fixture`, `tags` and `timeout_seconds` to appear
before child tables like `[cases.reference]`.

## Input bundles

Use `[cases.input_bundle]` for joins, lookups and other callables that consume two or three frames.
Each `[cases.input_bundle.inputs.<name>]` contains a `fixture`, a nested `schema`, or both. Names must
be Python identifiers. With the default `binding = "keyword"`, Parity invokes
`fn(**frames, **static_kwargs)` and rejects positional static arguments or colliding keywords.
`binding = "positional"` invokes frames in declared TOML order before `static_args`.

```toml
[cases.input_bundle]
binding = "keyword"

[cases.input_bundle.inputs.orders]
fixture = "tests/fixtures/orders.arrow"

[cases.input_bundle.inputs.customers.schema]
min_rows = 1
max_rows = 20

[[cases.input_bundle.inputs.customers.schema.columns]]
name = "customer_id"
dtype = "int64"
nullable = false
unique = true

[[cases.input_bundle.relationships]]
kind = "foreign_key"
child = { input = "orders", columns = ["customer_id"] }
parent = { input = "customers", columns = ["customer_id"] }
allow_nulls = true
```

Relationships are generated and shrunk jointly:

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
only non-null child keys must occur in the parent. A fixture-only input has its schema inferred
before relationship validation. Input fixtures are all-or-none: either provide a fixture for every
input or use schemas for the generated bundle.

## Callable specification

Both `[cases.reference]` and `[cases.candidate]` accept:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `target` | string | required | Import target `package.module:function`; each dotted component must be a Python identifier. |
| `adapter` | enum | `auto` | `auto`, `pandas`, `polars` or `arrow`. |
| `pandas_input` | `arrow` / `native` | `arrow` | Pandas input materialization; ignored when the resolved adapter is not pandas. |
| `python` | path | current Python | Interpreter for isolated execution. |
| `workdir` | path | config directory | Working directory and import root. |
| `environment` | string table | `{}` | Literal environment overrides for the worker. |
| `record_distributions` | string array | `[]` | Additional distribution versions to record inside this worker. |

Paths may be relative. A configured `python` path is anchored to the configuration directory
without dereferencing its final virtual-environment symlink; two venv entry points that share one
base Python therefore remain distinct execution environments. Other project paths retain their
normal resolved semantics. Do not commit secrets in `environment`.

Parity always records its own version plus Python, platform, Hypothesis, NumPy, pandas, Polars and
PyArrow provenance. `record_distributions` adds up to 64 explicitly named Python distributions,
using distribution names rather than import names (for example `scikit-learn`, not `sklearn`).
Names are normalized and duplicates are errors. This field only reads installed metadata; it never
installs or imports the named distribution.

`pandas_input = "arrow"` converts the canonical input to pandas with Arrow extension dtypes. It
preserves distinctions such as nullable integers and Arrow null versus a valid IEEE NaN, and is the
stable default. `pandas_input = "native"` uses PyArrow's default pandas conversion for legacy code
that expects conventional NumPy/object dtypes. Native conversion is pandas-version-dependent and
can widen nullable integers or collapse null and NaN into the same missing value. This setting
changes only the callable's input; returned pandas objects still use Parity's normal Arrow
canonicalization.

## Frame schema

`[cases.schema]` accepts:

| Key | Type | Default | Constraint |
|---|---:|---:|---|
| `min_rows` | integer | `0` | At least 0. |
| `max_rows` | integer | `30` | From `min_rows` through 10,000. |
| `unique_together` | array of string arrays | `[]` | Each named tuple must be unique. |
| `constraints` | array of tables | `[]` | Frame-local valid-domain constraints described below. |
| `columns` | array of tables | required | One or more unique columns. |

Declare a column with `[[cases.schema.columns]]`:

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
| `timezone` | string | none | IANA timezone metadata for datetime columns. |

Portable dtype families are `integer`, `float`, `decimal`, `boolean`, `string`, `category`,
`binary`, `date`, `datetime`, `time`, `duration`, `list`, `struct` and `object`. Common concrete
integer/float widths such as `int8`, `int64`, `uint32`, `float32` and `float64` are retained when
materialising Arrow inputs. Bounds must be representable by the chosen type.

### Frame constraints

Declare each constraint with `[[cases.schema.constraints]]`, or under an input schema such as
`[[cases.input_bundle.inputs.left.schema.constraints]]`.

| `kind` | Fields | Meaning |
|---|---|---|
| `sorted_by` | `columns`, `descending=false`, `nulls="last"` | Generate the complete frame in lexicographic order by one or more columns. `nulls` is `first` or `last`. |
| `row_comparison` | `left`, `operator`, `right` | Require the named columns to satisfy `lt`, `le`, `eq`, `ge` or `gt` on every row where both values are non-null. |

```toml
[[cases.schema.constraints]]
kind = "sorted_by"
columns = ["account_id", "event_time"]
descending = false
nulls = "last"

[[cases.schema.constraints]]
kind = "row_comparison"
left = "start_time"
operator = "le"
right = "event_time"
```

At most one `sorted_by` constraint is accepted per frame. Referenced columns must exist and row
comparisons must use compatible, orderable types. Constraints are preserved by deterministic
cases, generated search, shrinking and relationship rewrites in input bundles; impossible domains
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

## Generation policy

`[cases.generation]`:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `max_examples` | integer | `100` | Property examples, 1 through 100,000. |
| `max_findings` | integer | `1` | Maximum distinct mismatch signatures, 1 through 20. Each additional search receives its own `max_examples` budget. |
| `stability_repeats` | integer | `2` | Total same-input observations per implementation for deterministic passing inputs, 1 through 10. `1` disables the stability check. |
| `search` | boolean | `true` | Run property-based search after deterministic inputs. Set this and `adversarial_examples` to `false` for an exact fixture-only contract. |
| `seed` | integer/null | none | Stable run seed. |
| `deadline_ms` | integer/null | none | Per-Hypothesis-example deadline. |
| `adversarial_examples` | boolean | `true` | Run deterministic edge cases before search. |
| `shrink` | boolean | `true` | Minimise a discovered failing input. |
| `derandomize` | boolean | `false` | Hypothesis deterministic derivation mode. |
| `suppress_too_slow` | boolean | `true` | Suppress Hypothesis health check for slow generation. |

A seed improves repeatability but a saved replay artifact is the strongest reproduction mechanism.
With `search = false`, Parity still checks deterministic fixtures or enabled adversarial examples,
including stability repeats, but skips property-based search beyond them. A searchless case without
any deterministic input is rejected instead of passing without evidence. Performance policy is
unchanged; set `[cases.performance] enabled = false` separately for a semantic-only fixture check.
Mismatch signatures classify observable difference shapes, not root causes or separate bugs. Every
generated witness is observed twice after shrinking; changing signatures or side-specific output
nondeterminism stops the campaign as an execution error rather than creating questionable evidence.
Separately, deterministic inputs that initially pass are observed `stability_repeats` times. Each
side is compared with its own first observation, so matching but equally unstable implementations
cannot produce a false pass. Repeat exceptions, crashes, timeouts or output drift stop the campaign
as an unsigned execution error before generated search or benchmarking.
`examples_run` and `deterministic_examples` count each input once; stability observations are
additional callable executions, not additional generated examples.

## Performance policy

`[cases.performance]`:

| Key | Type | Default | Meaning |
|---|---:|---:|---|
| `enabled` | boolean | `true` | Benchmark only after semantic success. |
| `warmups` | integer | `1` | Warm-up pairs, 0 through 100. |
| `repeats` | integer | `5` | Measured pairs, 1 through 1,000. |
| `max_slowdown` | float/null | `1.25` | Candidate/reference runtime ratio limit. |
| `max_memory_ratio` | float/null | `1.5` | Candidate/reference peak RSS ratio limit. |
| `min_reference_ms` | float | `1` | Ignore runtime ratio below this reference duration. |
| `fail_on_regression` | boolean | `false` | Convert a policy regression into case failure. |

Runtime and memory ratios are evidence from the current host, not portable guarantees.

## Complete generated template

Run `parity init`, or call `parity.templates.render_config_template()`, for a versioned and
validated example containing every policy field. To generate one minimal fixture-backed case for
existing code, supply `--reference`, `--candidate` and `--fixture` together or call
`parity.templates.render_project_config()`. The project form omits default-valued tables and does
not create an implementation module.
