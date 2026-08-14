# Architecture

## System boundary

Parity is a local verifier around user-owned computations. It is not a dataframe execution engine,
an application runtime or a UI layer. The canonical input and result contracts are defined as
Python models; adapters isolate engine details.

```text
parity.toml / Python API
          │
          ▼
  validated campaign ───── fixture/schema
          │                      │
          │                      ▼
          │              adversarial + generated
          │                 Arrow inputs
          ▼                      │
 reference worker ◀──────────────┼──────────────▶ candidate worker
          │                      │                       │
          └──────── observations┴observations ─────────┘
                                 │
                                 ▼
                      canonical comparison
                                 │
                     pass ───────┴────── mismatch
                       │                    │
                       ▼                    ▼
              interleaved benchmark   shrink + diagnose
                       │                    │
                       └──────────┬─────────┘
                                  ▼
                     result models + artifacts
```

## Layers

### Contracts and configuration

Pydantic models reject unknown fields and validate constraints before code executes. Configuration
paths are resolved against the TOML location. `SuiteResult`, `CaseResult`, structured mismatches,
diagnoses and performance models are usable by the CLI, pytest and downstream automation.

The migration coverage layer has a separate strict manifest. Each declared unit maps to one or
more configured case names, carries an explicit exclusion, or remains visibly uncovered. The gate
cross-validates all mapped names before execution, runs their union once and derives unit status
from case evidence. It does not execute metadata from the manifest, discover APIs or infer that a
case genuinely covers its declared unit.

Migration execution deliberately ignores configured `fail_fast` so every mapped case is attempted.
A missing, skipped, zero-example or errored case is uncertain evidence and makes the unit an error.
At least one unit must pass, preventing an empty or all-excluded inventory from succeeding. The
ordinary `parity check` uses the case configuration and suite-result contracts directly.

### Adapters and Arrow boundary

Every input is represented canonically as an Apache Arrow table. An adapter converts it to pandas,
Polars or Arrow at the callable boundary and converts supported outputs back to canonical semantic
values. This reduces pairwise conversion from an engine-by-engine matrix to one adapter per engine.

Arrow is a transport representation, not the definition of equivalence. Comparison retains semantic
families and applies explicit policies for concrete dtypes, names, order, missing values, numerical
tolerances and temporal values.

Output rows have three deliberately separate comparison modes. Strict mode is positional; ignore
mode treats rows as a multiplicity-preserving bag; keyed mode resolves a declared unique composite
key, aligns the two outputs by exact key identity, and then uses the ordinary cell policy. Keeping
key identity outside tolerance matching prevents a wider numeric tolerance from silently pairing
different business entities. Duplicate or unavailable keys fail closed rather than selecting an
arbitrary pairing.

### Generation

Schema-aware deterministic cases run first so common faults have stable names and reproduce without
random search. Hypothesis then explores and shrinks the remaining domain. `unique` and
`unique_together` constraints are enforced in strategies. Frame-local ordering and row-comparison
constraints are constructed as part of the strategy and revalidated after relational bundle key
rewrites, so search and shrinking remain inside the declared valid domain. Generated tables
preserve types even when empty.

Fixture-only cases infer a portable schema. Explicit schemas are preferable for high-value contracts
because sample inference cannot know business bounds or invariants.

### Execution

Callables are observed rather than invoked directly by the comparator. An observation records
returned value or exception, elapsed time, peak RSS, mutation state, timeout/worker status and safe
diagnostic metadata. It also records bounded runtime provenance from inside that worker: Python,
platform, Parity, core dataframe dependencies and explicitly requested target distributions.
Configured Python executables and working directories support A/B dependency environments.

Configured campaigns keep two worker sessions alive: one for the reference and one for the
candidate. This removes interpreter startup from every generated example while preserving a crash
and state boundary *between the two implementations*. Every invocation is delivered through a new
private Arrow/JSON call directory and is adapted into a fresh input object.

Module globals, import caches, threads and other process state intentionally persist from one
example to the next within each session, including into performance warmups and repeats. Callables
should therefore be deterministic functions of their explicit inputs and static arguments. A
timeout, native crash or invalid protocol response terminates the affected worker and marks its
session unusable; it is never silently restarted with reset state. Campaign teardown explicitly
terminates both process groups and their descendants. The lower-level `execute_isolated` API remains
available when a fresh disposable process is required for every invocation.

Counterexample discovery benefits from those persistent sessions, but accepted configured findings
do not rely on their accumulated state. Deterministic witnesses are repeated once in a fresh worker
pair, and generated witnesses are repeated twice with a new worker pair for each observation.
Importable live callables use the same clean confirmation before their evidence is called replayable.
Non-importable live callables cannot be reconstructed in a fresh process and retain same-process
confirmation; their artifacts are evidence-only and explicitly reject automatic replay.

Deterministic inputs that pass pairwise comparison are also repeated according to
`stability_repeats`. Each implementation is compared against its own first observation. This catches
matching hidden state, unstable reductions and repeated-call failures that ordinary differential
comparison could otherwise label as a pass. Any instability is an execution error and blocks
generated search and performance measurement.

Worker processes are not a hostile-code sandbox.

### Comparison and diagnosis

Canonicalisation converts supported frames, series, arrays, mappings, sequences and scalars to
engine-neutral structures. The comparator accumulates structured mismatches rather than throwing at
the first unequal value. Row-order-insensitive comparison retains duplicate multiplicity.

Diagnoses are deterministic rules backed by observed mismatch kinds and values. They never alter
the outcome and never send code or data to an LLM.

### Performance

Benchmarking begins only after semantic success. Reference and candidate runs alternate order to
reduce systematic first/second bias; medians reduce single-iteration noise. Runtime ratios below
`min_reference_ms` are ignored. Peak RSS is process evidence and includes interpreter/runtime
effects.

### Artifacts and reporting

The writer creates a new timestamped directory for each counterexample and does not overwrite an
existing directory:

```text
<artifact-root>/<safe-case>/<UTC timestamp>-<input hash>/
  input.arrow  # single-frame case
  input.parquet  # present when representable
  # or input-000.arrow, input-001.arrow, ... for a bundle
  manifest.json
  result.json
  replay.json
```

Arrow IPC files are the lossless replay authority; Parquet is only a human/tooling convenience and
is omitted for schemas it cannot represent. Bundle filenames are opaque: `replay.json` binds each
file to its logical input name. `manifest.json` binds case/config metadata, hashes and
artifact schema. `replay.json` contains the
sanitized executable contract and argument vector needed to reproduce the case, and records that
replay must be launched from the original project invocation directory. Import roots outside that
directory are deliberately non-replayable rather than replaced with a potentially different
same-named module. Reports are separate projections that omit frame and value data. Artifact writes
use a temporary directory and final rename so interrupted runs do not look complete.

Manifest contract 1 hash-binds every artifact file. Replay contract 1 represents one to three named
inputs through a single `inputs` list; a single-frame case uses the reserved logical name `input`.
Every automatic replay binds the worker runtime fingerprints and data-safe
effective-configuration hash. Replay probes both workers before target import and blocks both
callables on drift or incomplete provenance. Evidence without those bindings remains inspectable
but is not executable through automatic replay. JSON report schema 3 carries data-free mismatch
signatures and distinct-finding counts.

The sanitized replay case stores the complete comparison policy, including keyed row alignment,
and its effective-configuration hash. Replay therefore reconstructs the recorded contract instead
of applying defaults from a different case.

Migration JSON is a separate report schema, currently version 1. It binds derived unit results to a
canonical migration-manifest hash and nests the data-safe suite report for the exact
mapped-case union; that report retains its effective configuration hash. Unit IDs, case names and
exclusion reasons pass through redaction. Compared values remain artifact-only. A migration report
therefore demonstrates the result for one declared inventory and configuration, not that the
inventory was exhaustive.

## Extension seams

- **Engine adapter:** Arrow conversion plus environment/runtime capability description.
- **Input provider:** fixture, generated schema, captured production shape or future contract source.
- **Comparator:** explicit semantic type with policy validation; never an opaque “close enough”.
- **Executor:** local process today, hardened container/remote worker later.
- **Reporter:** consumes result models and must declare its data-redaction level.

These seams make engine-neutral growth possible without turning the core into a workflow platform.

## Versioning

Five independently versioned contracts matter:

1. TOML configuration version.
2. Migration-manifest version.
3. Pydantic suite-result/report schema and package version.
4. Migration-report schema version.
5. Counterexample manifest/replay artifact version.

Before `1.0`, the latest minor release is the supported line and minor releases may change these
contracts. Readers reject unsupported contract versions rather than guessing how to interpret
them. Patch releases preserve the current minor's contracts. Runtime fingerprints detect drift but
are not environment lockfiles or container attestations; projects should still pin dependencies
for release gates.
