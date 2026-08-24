# Architecture

## System boundary

Parity is a local behavioural compatibility engine around user-owned computations. The controller
owns generation, comparison, findings and artifacts; the surrounding project owns migration
authoring, target provisioning, application execution and presentation. A versioned process
protocol keeps target environments and languages independent of the controller implementation.

```text
parity.toml / Python API
          │
          ▼
  validated campaign ───── invocation contract/custom generator
          │                      │
          │                      ▼
          │              adversarial + generated
          │              complete invocations
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

Every input is represented by one canonical `Invocation`: an ordered positional tuple and keyword
mapping containing Arrow tables, portable JSON-like values and homogeneous frame sequences.
Built-in adapters materialize every Arrow leaf as pandas, Polars or Arrow at the callable boundary,
then invoke both sides with exactly `callable(*args, **kwargs)`. Small project wrappers map that
shared call into unrelated APIs or domain objects. A per-target output canonicalizer can map a
successful raw return into the shared Arrow/JSON semantic contract.

The two endpoint boundaries accept the same invocation shape; their wrapped internal APIs can be
completely different. A canonicalizer is applied only to successful returns, so target-raised
exceptions remain observable behaviour.

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
constraints are constructed as part of the strategy and revalidated after relational invocation
rewrites, so search and shrinking remain inside the declared valid domain. Unrelated frames, JSON
choices, frame sequences and expanded varargs are composed into one jointly shrinking call.
Generated tables preserve types even when empty.

Fixture-only cases infer a portable schema. Explicit schemas are preferable for high-value contracts
because sample inference cannot know business bounds or invariants.

A project generator is a deliberately small alternate input-provider seam. Its importable factory
runs in the driver and returns a Hypothesis strategy or bounded iterable of complete `Invocation`
objects. Plain iterables are deterministic corpora rather than property strategies and do not
claim shrinking. Both forms feed the ordinary comparison, finding, artifact and exact-replay
pipeline.

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
and the application. A command target is any executable implementing protocol v2. Both forms use a
new private Arrow/JSON call directory for every invocation and adapt every frame into a fresh input
object.

Preflight has two phases. `runtime` proves that both transports work and checks runtime identity
plus declared dependency requirements without importing user targets. Only after both sides pass
does `inspect` validate the targets, adapters and output canonicalizers, still without invoking
application behaviour. A peer blocked by a transport failure is explicitly `not_checked`. This
makes setup failure an actionable infrastructure `ERROR`, never behavioural evidence. The full
contract is in [the target protocol](TARGET_PROTOCOL.md).

Module globals, import caches, threads and other process state intentionally persist from one
example to the next within each session, including into performance warmups and repeats. Callables
should therefore be deterministic functions of their explicit invocation. A
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
  input-000.arrow, input-001.arrow, ...  # one per Arrow leaf; none for zero-frame calls
  input-000.parquet, ...  # convenience copies when representable
  manifest.json
  reference.json
  reference.arrow  # or reference-value.json for a JSON-compatible return
  result.json
  replay.json
```

Arrow IPC files are the lossless replay authority; Parquet is only a human/tooling convenience and
is omitted for schemas it cannot represent. Input filenames are opaque: `replay.json` recursively
binds each file to its positional, keyword or frame-sequence location and retains JSON values.
`manifest.json` binds case/config metadata, hashes and
artifact schema. `replay.json` contains the sanitized executable contract and argument vector
needed to reproduce the case. Replay v3 records the project base as a bounded ancestor count from
the artifact. The reader walks from the artifact and never falls back to `Path.cwd()`, so a complete
project tree remains replayable after relocation. Configured runs use the directory containing the
loaded `parity.toml`; direct live runs use their invocation directory. Import roots, interpreters,
workdirs and path-like commands outside that base are deliberately non-replayable rather than
replaced with a potentially different same-named module or runtime. An optional top-level
`replay_blockers` map preserves a bounded per-side `live_callable`, `external_python`,
`external_workdir`, `external_command` or `missing_command` reason without preserving an external
path, so replay can give an actionable repair.
Confirmed semantic findings additionally bind the complete reference observation in private
`reference.json` plus an Arrow or JSON output file. This is the source for contract distillation;
it is never projected into a data-safe report.
Managed wrappers import from the workspace directory, and that directory plus its generated
environments must be contained by the configured `parity.toml` directory. Other managed layouts
are rejected before setup so automatic replay paths cannot silently become external.
Reports are separate projections that omit frame and value data. Artifact writes use a temporary
directory and final rename so interrupted runs do not look complete.

Manifest contract 3 hash-binds every artifact file. Replay contract 3 stores the complete recursive
invocation, including zero-frame calls, JSON arguments, many fixed call slots and frame sequences.
Every automatic replay binds target runtime fingerprints and the data-safe effective-configuration
hash. Replay preflights both target sessions and blocks both implementations on drift or incomplete
provenance. Evidence without those bindings remains inspectable but is not executable through
automatic replay. JSON report schema 4 carries data-free mismatch signatures, approval state,
compatibility-budget outcomes and distinct-finding counts.

Distilled-contract manifest 3 is a separate candidate-only boundary. `contract distill` verifies
signed report artifacts, copies their minimized inputs and reference observations into a new atomic
private directory, and removes the reference endpoint entirely. `contract verify` validates every
bound file, reconstructs only the project-relative candidate, starts a fresh candidate process per
example and compares the result with the stored observation. It does not generate inputs, benchmark
or import a reference. `contract retire` checks candidate differences against the report-bound
compatibility budget, requires two exact stable candidate observations, and creates a second
candidate-baseline contract bound to the prior contract digest. See
[Distilled contracts](DISTILLED_CONTRACTS.md) and
[Compatibility budgets](COMPATIBILITY_BUDGETS.md).

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

- **Target adapter:** canonical invocation-to-API mapping plus successful-output canonicalisation.
- **Input provider:** fixture, generated schema, captured production shape or future contract source.
- **Comparator:** explicit semantic type with policy validation; never an opaque “close enough”.
- **Executor:** portable Python and arbitrary local protocol commands today; containers, remote
  workers and additional resource controls later.
- **Observation:** return, raise, mutation and process metrics today; isolated effect evidence later.
- **Reporter:** consumes result models and must declare its data-redaction level.

These seams make engine-neutral growth possible without turning the core into a workflow platform.

## Versioning

Nine independently versioned contracts matter:

1. TOML configuration version.
2. Migration-manifest version.
3. Pydantic suite-result/report schema and package version.
4. Migration-report schema version.
5. Compatibility-budget version.
6. Distilled-contract version.
7. Counterexample manifest/replay artifact version.
8. Target process-protocol version.
9. Mismatch-shape fingerprint version (`ms3`).

Before `1.0`, the latest minor release is the supported line and minor releases may change these
contracts. Readers reject unsupported contract versions rather than guessing how to interpret
them. Patch releases preserve the current minor's contracts. Runtime fingerprints detect drift but
are not environment lockfiles or container attestations; projects should still pin dependencies
for release gates.
