# Use cases and boundaries

Parity fits when two implementations can be given the same reviewable input contract and their
observable outcomes can be compared. The implementations do not need matching source, APIs,
dependencies, runtimes or languages.

The contract is a complete call: zero or more positional and keyword JSON/frame values followed by
a canonical return or domain exception. Built-in frame schemas cover tabular structures, while
project-owned generators can produce bounded domains such as recursive syntax trees and stateful
event streams.

## Supported now

| Migration | Typical endpoints | Contract boundary |
|---|---|---|
| Dependency or Python upgrade | Released packages and/or local checkouts | Separately locked target environments |
| Release or branch regression | Stable baseline and local/published candidate | Fixtures, generated inputs and replay |
| Large refactor or API redesign | Side-specific Python wrappers | Canonical Arrow/JSON result |
| Rules engine, parser or protocol rewrite | JSON requests, recursive programs or event streams | Nested values, domain exceptions and mutation |
| pandas, Polars or backend change | Built-in or project-owned adapters | Explicit order, dtype, null and tolerance policy |
| Python rewrite | Unrelated modules/packages | Portable worker in each environment |
| Cross-language rewrite | Python and/or protocol-speaking commands | Versioned Arrow/JSON target protocol |
| Numerical/scientific migration | Python or command targets | Special values, exceptions, tolerances and optional performance evidence |
| Generated-code verification | Handwritten reference and generated candidate | The same reviewed release gate |

Every entry uses the same campaign, comparison, finding and replay engine.

### Dependency, runtime and source changes

A managed workspace supports released/released, released/local, local/released and local/local
pairs. Each released source is an exact `package==version`; each checkout is installed only into
its own target environment. Locks remain stable until `--refresh-locks` deliberately changes
dependency selection.

For local sources, Parity verifies import origins and records path-free Git/content identity.
Local/local runs also detect source changes during the campaign. Parity does not create, switch or
edit branches and worktrees.

The same isolation is available without a workspace by setting a different `python` executable on
each endpoint. Target environments need PyArrow, the selected adapter dependency and the
application—not the full controller dependency tree.

### Changed APIs and architectures

Small project-owned wrappers map canonical inputs into each API. One side may call
`old.calculate(x, y)` while the other constructs `Engine().calculate(Data(x, y))`. A configured
output canonicalizer can project a successful domain object into a shared Arrow or JSON-like
result.

This explicit adapter boundary is part of the reviewed contract, not a workaround. Keep it small:
complex wrappers can hide the very migration defect being tested.

### Cross-language and legacy replacements

Any executable that implements the [target protocol](TARGET_PROTOCOL.md) is a first-class endpoint.
The adapter receives the shared Arrow/JSON invocation, invokes the native program and returns a
canonical value, domain
exception or infrastructure error. The program may be Fortran, C/C++, Rust, Java, another Python
runtime or a legacy CLI.

The wrapped program does not link to Parity. A small Python boundary can use the
[command-adapter SDK](TARGET_ADAPTER_SDK.md); a non-Python adapter can implement the protocol
directly. Compilation, image construction and dependency installation remain outside Parity.

The maintained [JavaScript-to-Python rules-engine proof](../case_studies/javascript_python_rules/README.md)
exercises recursive JSON generation, keyword arguments, nested returns, domain exceptions,
multi-finding shrinking and replay through a complete JSON call contract.

### Regression and performance gates

Parity can sit beside unit tests as a differential regression suite:

- fixtures preserve important structures;
- schemas and custom generators explore a wider domain;
- stable controls expose broken setup or over-broad policies;
- distinct findings retain independently minimized witnesses; and
- replay promotes a discovered failure into permanent evidence.

After semantic success, Parity can compare paired elapsed time and peak process RSS. Use a
controlled host, enough repeats and `jobs = 1` for an enforced or retained performance claim. This
is regression evidence, not a general microbenchmark framework.

## Supported through explicit projection

A reviewed adapter can make an effect part of the returned contract, for example:

- run a CLI and return normalized exit code/stdout/stderr;
- execute an isolated database transaction and return resulting rows;
- render a generated file and return its bytes or metadata;
- capture emitted events and return their canonical sequence; or
- seed a stochastic algorithm and return deterministic outputs/statistics.

Parity will compare, shrink and replay that returned value. It does not independently isolate,
capture or restore the underlying effect, so the adapter and fixture own cleanup and evidence
fidelity. Do not describe this as first-class filesystem, database or network capture.

## Poor fits today

Parity should not be the primary tool when:

- correctness requires a formal proof rather than sampled behavioural evidence;
- no shared input/output/effect contract can be reviewed;
- the target is hostile code requiring a security sandbox;
- behaviour cannot be made deterministic or given a defensible statistical policy;
- production side effects cannot be isolated and replayed; or
- the goal is to synthesize, plan or automatically repair a migration.

Parity does not decide which side expresses business intent. It can verify code produced by a
human, codemod or AI agent, but the migration author and reviewer still own scope and policy.

First-class effect capture, container/remote executors and stateful operation-sequence generation
are possible future extensions, not implied by the current process protocol. See the
[roadmap](ROADMAP.md) for the design criteria.
