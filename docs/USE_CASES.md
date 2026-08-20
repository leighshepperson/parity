# Use cases and boundaries

Parity verifies a shared behavioural contract; it does not require two implementations to share
source, APIs or runtime. This page distinguishes what is first-class now, what works through an
intentional contract adapter, and what remains future work.

## Supported now

| Use case | Reference and candidate | Contract boundary |
| --- | --- | --- |
| Dependency upgrade | Exact released packages and/or local checkouts | Separately locked portable Python workers per side |
| Release regression | Published baseline and published or local candidate | Fixtures/generated inputs, findings and replay |
| Branch or worktree comparison | Two existing local checkouts | Managed local/local workspace with Git/content provenance |
| Large refactor | Old and new Python callables | Small side-specific API adapters and output canonicalizers |
| Backend replacement | Different dataframe/numerical engines | Canonical Arrow input plus explicit comparison policy |
| Python rewrite | Unrelated Python modules or packages | Portable worker in each target environment |
| Cross-language rewrite | Any two protocol-speaking executables | External command target protocol with Arrow/JSON transport |
| Numerical/scientific migration | Python or command targets | Boundaries, special values, tolerances, exceptions and performance CIs |
| Generated-code verification | Handwritten reference and generated candidate | The same deterministic cases, artifacts and release gate |

These all use the same engine. Parity does not have a special “regression mode”, “AI mode” or
language-specific comparison core.

### Dependency and runtime changes

Set a different `python` interpreter on each side or use a managed migration workspace. Only the
portable worker requirements and target dependencies enter those environments; the full
`parity-check` dependency tree stays in the controller. This is the intended path for conflicting
dependency graphs and older supported Python targets.

Managed declarations are symmetric: combine `--reference-package` or `--reference-path` with
`--candidate-package` or `--candidate-path` for released→released, released→local,
local→released or local→local checks. Exact release declarations use an unconditional,
non-wildcard `package==version` requirement and are enforced on the corresponding target before
import. Separate locks are reused until `--refresh-locks` deliberately updates dependency
selection. For a simple same-API package upgrade, `migration init --target ... --fixture ...` can
create the first reviewed case contract, ledger and workspace together; side-specific target flags
cover renamed APIs.

Declare relevant `record_distributions` for provenance and `required_distributions` for fail-closed
preconditions. A version difference is allowed; unexpected drift from the recorded/replay contract
is not.

### Local branch, worktree and source comparisons

`parity migration init --reference-path ... --candidate-path ...` accepts two existing checkouts. Parity
does not create, select, switch or edit branches/worktrees. It provisions each checkout separately,
verifies installed import origins, records path-free revision/dirty/content identities and rejects
source changes during the campaign.

Every local target reports a path-free Git/content identity that findings and replay bind, so a
local source must be a Git worktree with committed HEAD. A mixed package/path workspace retains its
one local target identity but does not emit the paired `source-provenance.json` or receive the
continuous two-worktree driver checks used by local/local runs.

This is useful beyond migrations: run it as a behavioural pull-request gate, compare an optimization
branch with `main`, check a mechanical codemod, or validate a proposed bug fix against stable
controls and intentionally changed cases.

### API and architecture changes

Use tiny project-owned functions to map canonical input into each API. One side can call
`old.calculate(x, y, currency)` while the other constructs
`Engine(currency).calculate(Data(x, y))`. Side-specific arguments cover simple cases; wrapper
functions cover structural changes. An output `canonicalizer` can project a successful domain
object into the shared Arrow/JSON result contract.

This is not a workaround. The explicit adapter boundary is how Parity avoids assuming API identity
and keeps migration logic out of its comparison engine. Keep the adapters reviewable: they define
the contract and can themselves contain defects.

### Cross-language and legacy replacement

An external command target is a first-class endpoint. A thin executable adapter speaks the
[versioned target protocol](TARGET_PROTOCOL.md), maps Arrow inputs into the target's native call,
and returns canonical Arrow/JSON or exception evidence. The underlying program may be Fortran,
C/C++, Rust, Java, another Python runtime or a legacy executable.

No Parity library is linked into the target. Protocol adaptation is required because two arbitrary
programs otherwise have no shared representation; it is a stable integration boundary rather than
a one-off harness.

### Alternative libraries and backends

The built-in Arrow, pandas and Polars adapters are conveniences, not the product boundary. A
canonical adapter can construct NumPy arrays, validation models, domain entities, query plans or
another library's objects before invocation. Custom Hypothesis strategies can generate the domain
while retaining shrinking.

This supports package/API migrations whose meaningful behaviour is expressible as canonical
returns, raises and mutation today. Historical campaigns have exercised numerical, dataframe,
machine-learning and validation-library changes; their exact versions belong in the case studies,
not the general configuration contract.

### Regression suites and stable controls

Parity can sit beside unit tests as a differential regression framework:

- fixtures preserve important production shapes;
- custom generators reuse domain strategies or corpora;
- known-stable controls detect broken setup and over-broad comparison policy;
- `max_findings` separates multiple unrelated incompatibilities;
- replay promotes minimized witnesses into permanent regression cases; and
- migration manifests ensure every declared surface is passed, excluded or visibly uncovered.

For rolling A→B→C→D upgrades, keep durable core controls and only the active adjacent-pair cases.
After promoting the candidate, advance the baseline and discard obsolete transition evidence if it
has no audit value. Parity is a verifier, not a migration-history database.

### Performance regressions

After semantic success, Parity can compare elapsed time and peak process RSS for Python or command
targets. Paired runs, alternating order and bootstrap confidence intervals reduce obvious
noise-driven failures. Use a controlled host, meaningful fixtures, sufficient repeats and a
threshold justified by the application. Run performance evidence with `jobs = 1`; concurrent cases
contend even when their performance policy is report-only. This is a regression signal, not a
general-purpose microbenchmark suite.

## Supported through explicit projection

Some behaviours can be tested now by making them part of the target's returned contract:

- render a generated file into bytes/metadata and return that summary;
- run a CLI and return its exit code plus normalized stdout/stderr;
- execute a database transaction in an isolated fixture and return the resulting rows;
- capture emitted events or logs in the adapter and return their canonical sequence; or
- seed a stochastic algorithm and return deterministic outputs/statistics.

This is appropriate when the projection *is* the reviewed behavioural contract. Parity will
generate, shrink, classify and replay the canonical result. It does not yet independently isolate,
capture or restore those effects, so the adapter/test fixture owns cleanup and evidence fidelity.
Do not describe such a campaign as first-class filesystem, database or network effect capture.

## Future contracts

The execution and observation boundaries are designed to add effect evidence without changing the
reference/candidate model. Potential future work includes first-class capture and comparison of:

- stdout and stderr;
- files created, removed or changed;
- database mutations and transaction boundaries;
- HTTP/network calls;
- subprocess invocations;
- structured logs/events and exit codes; and
- stateful operation sequences with deterministic shrinking and replay.

Remote workers, containers and resource-control backends are also plausible executor extensions.
They are not implied by the local process protocol today.

## Poor fits today

Parity is not currently the right primary tool when:

- correctness is a theorem rather than a sampled behavioural property;
- there is no reviewable shared input/output/effect contract;
- the target is hostile code requiring a security sandbox;
- behaviour is inherently nondeterministic and cannot be seeded or given a statistical policy;
- comparison requires live production side effects that cannot be isolated and replayed; or
- the goal is to synthesize, plan or automatically repair a migration.

An AI migrator may use Parity as its deterministic verification layer. Keeping generation of code
and judgment of business intent outside the core lets the same evidence serve humans, CI systems,
codemods and future migration tools.
