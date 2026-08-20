# Roadmap

Parity is an open-source, local-first behavioural compatibility engine. This roadmap describes
technical direction, not release dates or commitments. Correctness, reproducible evidence and a
clean contract boundary take priority over feature count.

## Foundation in the current release

The current architecture establishes the core verification loop:

- canonical fixtures, deterministic boundary cases and Hypothesis generation/shrinking;
- first-class semantic Return/Raise outcomes with normalized exception evidence;
- independently classified `ms3:` findings, integrity-bound artifacts and exact replay;
- a dependency-light Python worker that does not install Parity into target environments;
- first-class arbitrary command targets over a documented Arrow/JSON protocol;
- small input adapters and per-side successful-output canonicalizers for changed APIs;
- project-owned Hypothesis strategies or bounded iterable generators;
- deterministic case-level concurrency plus opt-in target native-thread limits;
- paired performance ratios with bootstrap confidence intervals;
- composable case files/defaults and side-specific arguments without hiding case identity or input
  contracts;
- batch verification of retained mismatch evidence from suite and migration reports;
- separately locked released/local and local/local migration workspaces, with local source
  provenance; and
- one active adjacent migration pair with reusable core controls rather than an append-only history.

The controller owns search, comparison, findings and artifacts. Targets own only translation into a
shared behavioural contract. That division must remain clear as the project grows.

## Make findings excellent

- Keep semantic `FAILED` separate from infrastructure `ERROR` in every CLI, report and API.
- Improve data-safe summaries so a large campaign says what changed and where without disclosing
  witness values or raw exception text.
- Separate unrelated incompatibilities while keeping signatures stable across irrelevant paths,
  addresses, timestamps, IDs and dependency-version strings.
- Preserve exact private evidence, deterministic deduplication, minimized witnesses and independent
  replay for every accepted finding.
- Add mismatch classes only from concrete migrations; do not present a signature or heuristic
  diagnosis as a root-cause proof.
- Keep evidence verification explicit about stale behaviour, corrupt artifacts, runtime drift and
  unverifiable execution.

## Harden target isolation and protocol boundaries

- Exercise portable Python workers against increasingly old target environments while keeping the
  controller on supported modern Python. Python 2 is practical only through a maintained protocol
  adapter; it must not compromise the current worker or controller.
- Build conformance fixtures for command adapters in several runtimes and document implementation
  templates only after their failure behaviour is tested.
- Improve process-group termination, timeouts and resource limits across supported operating
  systems.
- Preserve strict two-phase preflight: transport/runtime first, endpoint imports/configuration
  second, no target invocation in either phase.
- Keep runtime/source identity path-free and replayable without pretending it is a cryptographic
  supply-chain attestation.
- Explore container or remote executors only behind the same target protocol and observation model.

## Broaden input domains without a proprietary language

- Improve built-in schemas for useful numeric boundaries, temporal ranges, time zones, regex/string
  constraints, enumerations and nullability.
- Extend cross-column/object constraints from demonstrated cases, such as ordered date ranges,
  low/high bounds and relational bundle invariants.
- Keep custom Hypothesis strategies first-class so domain shrinking is not lost.
- Keep bounded iterable providers compatible with existing corpora and domain-specific generators.
- Add compact declarative vocabulary only when it remains understandable in code review; do not
  recreate a general programming language in TOML.

Schema or constraint inference by AI is outside core Parity. A separate tool may suggest a reviewed
generator or schema and then use Parity to verify it.

## Parallel discovery and reproducible scheduling

Case-level parallelism remains the default unit because cases already own independent sessions,
seeds and artifact directories. Search and shrinking inside one case remain serial.

A later two-stage model may run several deterministic counterexample-discovery workers, deduplicate
their findings, and then shrink accepted classes serially. It should be added only with reproducible
seeds, bounded resources and tests showing scheduling cannot change accepted evidence. Performance
measurement must remain isolated from competing case jobs, especially for BLAS/OpenMP workloads.

## Observable effects

The observation model should evolve beyond pure input-to-output functions without becoming a
general workflow platform. Candidate effect contracts include:

- stdout/stderr and exit code;
- files created, removed or changed;
- database mutations;
- HTTP/network calls;
- subprocess invocations; and
- structured logs/events.

Today a reviewed adapter can project such effects into a canonical return, but Parity does not own
their isolation, cleanup or capture. First-class support requires deterministic evidence,
comparison, shrinking and replay semantics for each effect class before implementation.

## Performance evidence

- Continue validating the profiler against deliberately CPU- and memory-heavy implementations, not
  sleep-based timing stand-ins.
- Report confidence intervals, sample counts and gate reasons consistently in terminal, Markdown,
  JSON and JUnit projections.
- Fail closed when an enforced metric has insufficient or unavailable evidence.
- Document runner controls and oversubscription hazards; target execution usually dominates, so do
  not optimize controller internals without profiles.
- Consider stronger robust statistics only when real campaigns demonstrate a failure of the paired
  bootstrap design.

## Adversarial development method

Every substantial hardening change should be exercised against real historical migrations:

1. include known-stable controls;
2. include several unrelated incompatibilities and mismatch classes;
3. let generation discover witnesses rather than supplying every failing example;
4. require shrinking where the provider supports it;
5. replay every independent finding;
6. inspect clustering, status and provenance; and
7. preserve a small permanent regression test for each engine defect found.

Large one-off harnesses are validation evidence, not the primary unit-test suite. Public case
studies should record exact historical versions; general documentation should not pin moving
examples that become stale.

## Architecture toward 2.x

Do not rewrite Parity in Rust now. Parity 1.x should stabilize implementation-neutral contracts:

- process orchestration and target protocol;
- canonical input/output and observation models;
- comparison and finding signatures;
- artifacts, runtime/source provenance and replay;
- scheduling, timeouts and resource policies; and
- Python generator/adapter APIs.

Once those boundaries have extensive real-world evidence, a future engine could move orchestration,
scheduling, protocol validation, comparison and artifacts to Rust while Python continues to own
Hypothesis, Python adapters, custom generators and the Python API. That is an option created by clean
interfaces, not a current objective.

## Before 1.0

- Version and document configuration, migration manifests, target protocol, suite/migration reports,
  artifacts/replay and mismatch signatures.
- Demonstrate stable controls, meaningful independent findings, shrinking, replay and provenance on
  several unrelated non-trivial migrations.
- Publish a supported controller/portable-target/platform matrix and reproducible release process.
- Keep unsupported contract versions and setup failures explicit and actionable.
- Finalize the small public contracts that merit 1.x compatibility guarantees.

## Non-goals

- Claiming that property-based comparison is a formal proof.
- Deciding which implementation expresses business intent.
- Automatically generating, planning or repairing a migration.
- Becoming an AI migration product; an AI migrator may consume Parity as a verification layer.
- Running hostile code as a security sandbox.
- Managing, switching or editing users' Git branches/worktrees.
- Becoming a general build system, dataframe engine, application runtime or dashboard.
- Growing a large proprietary input-description language.
- Rewriting the engine in Rust before the behavioural contracts are stable.
- Preserving awkward pre-1.0 design decisions when a clean break materially improves the engine.

## Choosing priorities

A proposal is strongest when it includes a public or synthetic reference/candidate pair, a stable
control, a semantic or infrastructure failure that current tests mishandle, and a small permanent
regression test. A narrow, well-evidenced improvement should beat a broad speculative feature.
