# Architecture

## System boundary

Parity is a local behavioural compatibility engine around user-owned computations. It is not a
dataframe execution engine, migration author, application runtime or UI layer. The controller owns
generation, comparison, findings and artifacts. A versioned process protocol keeps target
environments and languages independent of the controller implementation.

```text
parity.toml / Python API
          │
          ▼
  validated campaign ───── fixture/schema/custom generator
          │                      │
          │                      ▼
          │              adversarial + generated
          │                 Arrow inputs
          ▼                      │
reference target ◀── target protocol ──┼── target protocol ──▶ candidate target
          │                      │                       │
          └──────── observations┴observations ─────────┘
                                 │
                                 ▼
                      canonical comparison
                                 │
                     pass ───────┴────── mismatch
                       │                    │
                       ▼                    ▼
              paired benchmark       shrink + classify
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

### Canonical contracts and adapters

Every input is represented canonically as an Apache Arrow table. Built-in adapters convert it to
pandas, Polars or Arrow at a Python callable boundary. Small project adapters may instead map the
canonical input into unrelated APIs or domain objects. A per-target output canonicalizer can map a
successful raw return into the shared Arrow/JSON semantic contract. This reduces pairwise
conversion from an implementation-by-implementation matrix to one boundary per target.

Reference and candidate signatures are deliberately independent. Shared and side-specific static
arguments cover simple differences; explicit wrapper functions cover architectural changes. A
canonicalizer is applied only to successful returns, so target-raised exceptions remain observable
behaviour.

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

A project generator is a deliberately small alternate input-provider seam. Its importable factory
runs in the driver and returns either a Hypothesis strategy or a bounded iterable. Strategy values
are converted to canonical Arrow *inside* the Hypothesis strategy, so dataframe conversion remains
part of generation and the resulting Arrow input still shrinks. Plain iterables are deterministic
corpora rather than property strategies and do not claim shrinking. Both forms feed the ordinary
comparison, finding, artifact and exact-replay pipeline.

### Execution

Targets are observed rather than invoked by the comparator. An observation records returned value
or exception, elapsed time, peak RSS, mutation state, timeout/process status and safe diagnostic
metadata. It also records bounded, path-free runtime and source provenance reported from the target
process. Configured Python executables, commands and working directories support unrelated A/B
dependency and runtime environments.

The comparison boundary projects observations into exactly two semantic outcomes: `Return(value)`
and `Raise(type, normalized message, allow-listed structured details)`. Return versus raise and
different raises are behavioural incompatibilities (`FAILED`). Canonicalization failure, invalid
protocol, unavailable target session, process crash and timeout are explicit execution outcomes
instead; they mean Parity could not compare behaviour and therefore produce `ERROR`. Classification never
depends on an exception class name, so a target-raised `TimeoutError` remains ordinary behaviour.

Exception messages are normalized before comparison. Witness literals, paths, object addresses,
timestamps, random identifiers and dependency versions do not destabilize findings. Stable API
subjects and structured validation reason codes still participate through opaque fingerprints, so
unrelated failures do not collapse. Reports and mismatch artifacts contain fingerprints and
allow-listed reason metadata, not raw caught exception text or tracebacks.

Configured campaigns keep two target sessions alive: one for the reference and one for the
candidate. A Python target runs through a dependency-light portable worker that imports no Parity,
Pydantic, Hypothesis, Rich or Typer; its environment needs PyArrow, the selected adapter dependency
and the application. A command target is any executable implementing protocol v1. Both forms use a
new private Arrow/JSON call directory for every invocation and adapt into a fresh input object.

Preflight has two phases. `runtime` proves that both transports work and checks runtime identity
plus declared dependency requirements without importing user targets. Only after both sides pass
does `inspect` validate the targets, adapters and output canonicalizers, still without invoking
application behaviour. A peer blocked by a transport failure is explicitly `not_checked`. This
makes setup failure an actionable infrastructure `ERROR`, never behavioural evidence. The full
contract is in [the target protocol](TARGET_PROTOCOL.md).

Module globals, import caches, threads and other process state intentionally persist from one
example to the next within each session, including into performance warmups and repeats. Callables
should therefore be deterministic functions of their explicit inputs and static arguments. A
timeout, native crash or invalid protocol response terminates the affected target process and marks
its session unusable; it is never silently restarted with reset state. Campaign teardown explicitly
terminates both process groups and their descendants. The lower-level `execute_isolated` API remains
available when a fresh disposable process is required for every invocation.

Counterexample discovery benefits from those persistent sessions, but accepted configured findings
do not rely on their accumulated state. Deterministic witnesses are repeated once in a fresh target
pair, and generated witnesses are repeated twice with a new target pair for each observation.
Importable live callables use the same clean confirmation before their evidence is called replayable.
Non-importable live callables cannot be reconstructed in a fresh process and retain same-process
confirmation; their artifacts are evidence-only and explicitly reject automatic replay.

Deterministic inputs that pass pairwise comparison are also repeated according to
`stability_repeats`. Each implementation is compared against its own first observation. This catches
matching hidden state, unstable reductions and repeated-call failures that ordinary differential
comparison could otherwise label as a pass. Any instability is an execution error and blocks
generated search and performance measurement.

Target processes are not a hostile-code sandbox.

Suite parallelism schedules complete cases in driver threads. A case continues to own one
reference/candidate session pair and performs its discovery and shrinking serially; this prevents
parallel shrink races and shared case state. Futures are placed back into declaration order before
building `SuiteResult`. Case names are unique, so concurrent artifacts remain in isolated
subdirectories. Parallel fail-fast is rejected rather than given timing-dependent semantics.
Native thread limits are opt-in and applied only to known BLAS/OpenMP environment variables in
target processes. Performance gates are not combined with concurrent case execution because target
contention would invalidate their uncertainty model.

### Comparison and diagnosis

Canonicalisation converts supported frames, series, arrays, mappings, sequences and scalars to
engine-neutral structures. The comparator accumulates structured mismatches rather than throwing at
the first unequal value. Row-order-insensitive comparison retains duplicate multiplicity.

Finding signatures use the versioned `ms3:` mismatch-shape contract. Exception signatures include
the ordered Return/Raise states, qualified exception types and normalized exception fingerprints.
They are deterministic deduplication keys, not root-cause claims, cryptographic signatures or
source attestations. Reports project safe reference/candidate outcomes and well-known qualified
types plus allow-listed Pydantic error codes/location shapes and NumPy API tokens. Custom
identifier-shaped metadata remains opaque; reports never copy raw exception messages or values.
Terminal findings print the complete replay signature.

Diagnoses are deterministic rules backed by observed mismatch kinds and values. They never alter
the outcome and never send code or data to an LLM.

### Performance

Benchmarking begins only after semantic success. Reference and candidate observations are paired
under nearby host load and alternate order to reduce systematic first/second bias. Parity reports
the median paired ratio plus a deterministic bootstrap confidence interval and an explicit gate
reason. It gates only when the interval's lower bound exceeds the policy threshold. Enforced gates
require enough observations; runtime ratios below `min_reference_ms` are ignored. Peak RSS is
sampled from the target process tree and includes interpreter/runtime effects. An enforced memory
policy fails closed if memory evidence is unavailable.

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
replay must be launched from its project base. Configured runs use the directory containing the
loaded `parity.toml`; direct live runs use their invocation directory. Import roots, interpreters,
workdirs and path-like commands outside that base are deliberately non-replayable rather than
replaced with a potentially different same-named module or runtime. An optional top-level
`replay_blockers` map preserves a bounded per-side `live_callable`, `external_python`,
`external_workdir`, `external_command` or `missing_command` reason without preserving an external
path, so replay can give an actionable repair.
Managed wrappers import from the workspace directory, and that directory plus its generated
environments must be contained by the configured `parity.toml` directory. Other managed layouts
are rejected before setup so automatic replay paths cannot silently become external.
Reports are separate projections that omit frame and value data. Artifact writes use a temporary
directory and final rename so interrupted runs do not look complete.

Manifest contract 1 hash-binds every artifact file. Replay contract 1 represents one to three named
inputs through a single `inputs` list; a single-frame case uses the reserved logical name `input`.
Every automatic replay binds target runtime fingerprints and the data-safe effective-configuration
hash. Replay preflights both target sessions and blocks both implementations on drift or incomplete
provenance. Evidence without those bindings remains inspectable but is not executable through
automatic replay. JSON report schema 3 carries data-free mismatch
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

- **Target adapter:** canonical input-to-API mapping plus successful-output canonicalisation.
- **Input provider:** fixture, generated schema, captured production shape or future contract source.
- **Comparator:** explicit semantic type with policy validation; never an opaque “close enough”.
- **Executor:** portable Python and arbitrary local protocol commands today; containers, remote
  workers and additional resource controls later.
- **Observation:** return, raise, mutation and process metrics today; isolated effect evidence later.
- **Reporter:** consumes result models and must declare its data-redaction level.

These seams make engine-neutral growth possible without turning the core into a workflow platform.

## Versioning

Seven independently versioned contracts matter:

1. TOML configuration version.
2. Migration-manifest version.
3. Pydantic suite-result/report schema and package version.
4. Migration-report schema version.
5. Counterexample manifest/replay artifact version.
6. Target process-protocol version.
7. Mismatch-shape fingerprint version (`ms3`).

Before `1.0`, the latest minor release is the supported line and minor releases may change these
contracts. Readers reject unsupported contract versions rather than guessing how to interpret
them. Patch releases preserve the current minor's contracts. Runtime fingerprints detect drift but
are not environment lockfiles or container attestations; projects should still pin dependencies
for release gates.
