# Architecture

## System boundary

Parity is a local verifier around user-owned computations. It is not a dataframe execution engine,
an application runtime or a UI layer. The canonical input and result contracts are stable Python
models; adapters isolate engine details.

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

### Adapters and Arrow boundary

Every input is represented canonically as an Apache Arrow table. An adapter converts it to pandas,
Polars or Arrow at the callable boundary and converts supported outputs back to canonical semantic
values. This reduces pairwise conversion from an engine-by-engine matrix to one adapter per engine.

Arrow is a transport representation, not the definition of equivalence. Comparison retains semantic
families and applies explicit policies for concrete dtypes, names, order, missing values, numerical
tolerances and temporal values.

### Generation

Schema-aware deterministic cases run first so common faults have stable names and reproduce without
random search. Hypothesis then explores and shrinks the remaining domain. `unique` and
`unique_together` constraints are enforced in strategies. Generated tables preserve types even when
empty.

Fixture-only cases infer a portable schema. Explicit schemas are preferable for high-value contracts
because sample inference cannot know business bounds or invariants.

### Execution

Callables are observed rather than invoked directly by the comparator. An observation records
returned value or exception, elapsed time, peak RSS, mutation state, timeout/worker status and safe
diagnostic metadata. Configured Python executables and working directories support A/B dependency
environments.

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

Each counterexample path is append-only by timestamp and input hash:

```text
<artifact-root>/<safe-case>/<UTC timestamp>-<input hash>/
  input.arrow
  input.parquet  # present when the Arrow schema is representable in Parquet
  manifest.json
  result.json
  replay.json
```

`input.arrow` is the lossless replay authority; Parquet is only a human/tooling convenience and is
omitted for schemas it cannot represent. `manifest.json` binds case/config metadata, hashes and
artifact schema. `replay.json` contains the
sanitized executable contract and argument vector needed to reproduce the case, and records that
replay must be launched from the original project invocation directory. Import roots outside that
directory are deliberately non-replayable rather than replaced with a potentially different
same-named module. Reports are separate projections that omit frame and value data. Artifact writes
use a temporary directory and final rename so interrupted runs do not look complete.

## Extension seams

- **Engine adapter:** Arrow conversion plus environment/runtime capability description.
- **Input provider:** fixture, generated schema, captured production shape or future contract source.
- **Comparator:** explicit semantic type with policy validation; never an opaque “close enough”.
- **Executor:** local process today, hardened container/remote worker later.
- **Reporter:** consumes result models and must declare its data-redaction level.

These seams make engine-neutral growth possible without turning the core into a workflow platform.

## Versioning

Three independently versioned contracts matter:

1. TOML configuration version.
2. Pydantic result/report schema and package version.
3. Counterexample manifest/replay artifact version.

Before `1.0`, breaking changes may occur with release notes and migration guidance. Long-term replay
requires storing artifacts together with an environment lockfile or container digest; source and
dependency drift can otherwise change the observed result.
