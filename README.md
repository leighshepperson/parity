# Parity

**Behavioural compatibility testing for software migrations.**

Parity tries to disprove that a candidate preserves the behaviour of a reference. It sends the
same canonical inputs to both sides, compares their observable outcomes, shrinks generated
counterexamples, classifies independent differences, and preserves evidence for exact replay.

```text
canonical inputs
      ├──> reference environment ──┐
      └──> candidate environment ──┴──> compare ──> shrink ──> persist/replay
```

The two sides do not need the same dependency versions, API, implementation, language, runtime or
architecture. They need a shared behavioural contract. That makes Parity useful for dependency
upgrades, refactors, branch/worktree regression testing, backend changes, rewrites and
cross-language replacements.

Parity is an open-source verification engine, not an AI migration product. A human, script or
migration agent can use its deterministic results as a gate, but Parity does not generate or repair
the candidate.

> Parity produces evidence, not a proof of equivalence. `PASSED` means no difference was found in
> the configured domain and search budget.

## Quick start

The controller requires Python 3.11 or later.

```bash
python -m pip install parity-check
parity --version
parity init
parity check
```

The base install uses Arrow and does not install pandas or Polars. Install
`parity-check[pandas]`, `parity-check[polars]`, or both extras when those adapters are part of the
contract. Managed released-package environments use `parity-check[workspace]`.

`parity init` creates a runnable `parity.toml` and example module. Replace the example functions
with small reference and candidate adapters around the behaviour you care about, then run
`parity check`. Parity handles process isolation, generation, shrinking, artifacts and reporting.

A compact real-project case is:

```toml
version = 1

[[cases]]
name = "orders"
fixture = "tests/fixtures/orders.parquet"

[cases.reference]
target = "migration_adapters:reference_orders"
adapter = "pandas"
record_distributions = ["orders-lib"]

[cases.candidate]
target = "migration_adapters:candidate_orders"
adapter = "polars"
record_distributions = ["orders-lib"]

[cases.comparison]
row_order = "keyed"
row_keys = ["order_id"]
dtype = "compatible"
rtol = 1e-7

[cases.generation]
max_examples = 250
max_findings = 4
seed = 20260820
```

Run the suite, one case, or a tag:

```bash
parity check
parity check --case orders --max-examples 1000
parity check --tag critical --json .parity/report.json --junit .parity/junit.xml
```

Exit `0` means `PASSED`, exit `1` means `FAILED` because behaviour or an enforced performance gate
differed, and exit `2` means `ERROR` because Parity could not perform a reliable comparison.

## What a run provides

- Fixtures, deterministic boundary cases, Hypothesis search and shrinking.
- First-class `Return(canonical_value)` and
  `Raise(exception_type, normalized_message, structured_details)` outcomes.
- Stable `ms3:` finding signatures that separate unrelated value, API and exception changes while
  ignoring volatile paths, addresses, timestamps, IDs and version text.
- Several independently confirmed findings per campaign, with deduplication and tiny witnesses
  where shrinking applies.
- Isolated reference and candidate processes, timeouts, mutation tracking and runtime/dependency
  provenance.
- Replayable Arrow inputs, integrity-bound manifests and the effective comparison contract.
- Ordered case-level parallelism without parallel Hypothesis shrinking.
- Median performance ratios with deterministic bootstrap confidence intervals and optional gates.
- Terminal, JSON, Markdown, JUnit and GitHub step-summary output.
- A CLI, Python API, pytest fixture and migration-inventory gate.

## Different APIs are normal

Reference and candidate functions do not need matching signatures. Put the translation at the
boundary and keep the canonical contract stable:

```python
def reference_quote(frame):
    # Import only the implementation available in the reference environment.
    from legacy import calculate as old_calculate

    row = frame.iloc[0]
    return old_calculate(row.x, row.y, row.currency)


def candidate_quote(frame):
    # Import only the implementation available in the candidate environment.
    from rewritten import Data, Engine

    row = frame.iloc[0]
    return Engine(row.currency).calculate(Data(row.x, row.y))
```

Shared `static_args`/`static_kwargs` and side-specific `reference_kwargs`/`candidate_kwargs` cover
simple declarative differences. For richer changes, these small project-owned functions are the
explicit adapters; Parity does not require old and new APIs to resemble each other.
When the sides have conflicting dependencies, keep their imports inside the side-specific
functions as above, or put the functions in separate modules. Importing both implementations at
the top of one shared module would make preflight fail in an environment that intentionally
contains only one of them.

If a target returns a domain object, add an output canonicalizer in that target environment:

```toml
[cases.candidate]
target = "migration_adapters:candidate_quote"
canonicalizer = "migration_adapters:quote_to_contract"
adapter = "arrow"
```

The canonicalizer receives the successful raw return value and produces an Arrow-compatible frame
or JSON-compatible value. Exceptions raised by the target remain semantic outcomes; a failure to
import or execute the adapter/canonicalizer is infrastructure `ERROR`.

## Target environments stay independent

For Python targets in another environment, set `python` on each side. The target environment needs
only PyArrow, the selected adapter dependency, and the application under test. It does **not** need
`parity-check`, Pydantic, Hypothesis, Rich or Typer. The controller launches a dependency-light
portable worker and validates transport, imports and requested distribution constraints before
invoking user code.

```toml
[cases.reference]
target = "migration_adapters:reference_orders"
python = ".venv-reference/bin/python"
adapter = "pandas"

[cases.candidate]
target = "migration_adapters:candidate_orders"
python = ".venv-candidate/bin/python"
adapter = "pandas"
```

This supports conflicting dependencies and older Python target environments without coupling them
to the controller's dependency graph. Use `parity doctor --config parity.toml` for two-phase
preflight: it validates both transports/runtimes and declared requirements before importing either
endpoint, then checks target, canonicalizer and adapter imports. It never invokes the target. If one
transport fails, the other endpoint is reported as `not_checked` with error code
`TargetEndpointNotChecked`, rather than producing misleading one-sided import evidence.

An arbitrary executable can instead be a first-class target:

```toml
[cases.reference]
command = ["./bin/legacy-adapter"]

[cases.candidate]
command = ["./bin/new-adapter", "--mode", "compatibility"]
```

Command targets implement the small versioned process protocol; they may be Python, Rust, Java,
C/C++, Fortran or anything else that can read/write JSON and Arrow IPC. See the
[target protocol](docs/TARGET_PROTOCOL.md) and the executable
[single-container Fortran-to-Python proof](case_studies/fortran_python/README.md).

## Inputs and domain generation

A fixture anchors the campaign in realistic structure. A reviewed schema then makes the explored
domain explicit: numeric and string bounds, nullability, enums, dates/datetimes, time zones, useful
examples, uniqueness, ordering and row relationships.

```toml
[cases.schema]
min_rows = 0
max_rows = 50

[[cases.schema.constraints]]
kind = "row_comparison"
left = "start_date"
operator = "le"
right = "end_date"

[[cases.schema.columns]]
name = "status"
dtype = "string"
nullable = false
categories = ["open", "closed"]
```

For domain objects or cross-object rules that do not fit the compact schema, use project-owned
generation code:

```toml
[cases.generation]
generator = "tests.generators:portfolios"
max_examples = 500
```

The factory may return a Hypothesis strategy, preserving shrinking through the ordinary finding
pipeline, or a bounded iterable from an existing corpus/generator. Parity deliberately keeps this
escape hatch first-class instead of growing `parity.toml` into a proprietary data language.

## Findings and replay

A mismatch creates an isolated directory:

```text
.parity/orders/<timestamp>-<input-hash>/
├── input.arrow
├── input.parquet        # when the schema is representable
├── manifest.json
├── replay.json
└── result.json
```

Multi-input cases use opaque Arrow filenames bound to their logical names in `replay.json`. Replay
checks artifact hashes, effective configuration and recorded runtime/source identities before
running trusted project code:

```bash
parity replay .parity/orders/<timestamp>-<input-hash>
parity evidence verify .parity/report.json --json .parity/evidence-status.json
```

`parity replay` preserves the finding's semantic status: a successfully reproduced
incompatibility is still `FAILED` and exits `1`. Use `parity evidence verify` when the question is
whether report-referenced findings reproduced; that command exits `0` when every one did.

Replay v2 binds interpreter, workdir and path-like command locations to an ancestor of the artifact
itself, so the same artifact command works from the project root, a sibling directory or an
unrelated current directory. Moving the complete project preserves that relationship; moving an
artifact by itself fails closed. If a live callable, external artifact root/interpreter/workdir/
command or missing local executable prevents reconstruction, the artifact remains inspectable and
replay reports the bounded reason and the concrete change needed to collect replayable evidence.

Each finding explains what class of behaviour changed and carries an `ms3:` mismatch-shape
fingerprint. It is a deterministic deduplication/replay key, not a cryptographic signature, source
attestation, root-cause claim or bug ID. Exception findings show data-safe reference/candidate
outcomes and well-known qualified types plus allow-listed Pydantic error codes/location shapes and
NumPy API tokens. Custom identifier-shaped metadata remains opaque; raw messages and witness
values remain private. Terminal output prints the complete replay signature.

## Parallelism and performance

Run independent cases concurrently:

```bash
parity check --jobs 8 --native-threads 1
```

Results return in configuration order and each case owns separate target sessions and artifact paths.
Search and shrinking inside a case remain serial and deterministic. `--native-threads` caps common
BLAS/OpenMP pools in target processes, avoiding jobs × native-thread oversubscription. Parallel
fail-fast is rejected because its result would depend on scheduling.

Performance starts only after semantic success. Parity alternates nearby reference/candidate
invocations, reports paired median speed and peak-memory ratios with deterministic bootstrap
confidence intervals, and fails an enforced gate only when the interval's lower bound exceeds its
threshold. Use `jobs = 1`, enough repeats and a controlled runner for meaningful performance
evidence; concurrent cases contend for the same host even when their performance policy is
report-only. Point estimates from busy shared hosts are not a defensible release gate.

## Managed migrations and local regression testing

For a declared library migration, keep the cases and any wrappers in `migrations/`. One initializer
can create the first fixture-backed case, starter inventory and workspace; review the generated
adapter contract and inventory before using either as a completion gate. The workspace prepares
separately locked environments and runs every lane:

```bash
python -m pip install "parity-check[workspace]"
parity migration init \
  --reference-package 'your-library==1.2.3' \
  --candidate-package 'your-library==2.0.0' \
  --scaffold \
  --json
# Review migration_adapters.py, fixtures/input.json, parity.toml and migration.toml,
# then mark the four checklist decisions resolved.
parity migration validate --json
parity migration run --json
```

`--scaffold` creates a deliberately incomplete Arrow adapter, tiny fixture, starter ledger and
four-item `migration.checklist.json`; it never overwrites any of them. `migration validate` loads
the workspace/config/ledger/fixtures without creating environments or invoking targets, exits `1`
while review decisions remain, and exits `0` only when the authored contract is structurally ready.
For an existing wrapper and fixture, use `--target` (or side-specific targets) plus `--fixture`
instead. Existing reviewed contracts are never overwritten; `--force` replaces only the workspace.

For a branch or worktree comparison, point both sides at existing checkouts (and either scaffold as
above or reuse an existing contract):

```bash
parity migration init \
  --reference-path ../main-worktree \
  --candidate-path ../feature-worktree
parity migration run
```

Each side is independently an exact released requirement or a local checkout, so released→released,
released→local, local→released and local→local checks use one workflow. The reference requires
exactly one package/path flag. Candidate package/path flags are mutually exclusive; omitting both is
shorthand for `--candidate-path .`. The saved workspace always contains exactly one source field per
side.
`migration init` creates the active workspace and, when needed, a starter ledger that maps every
configured case to `core-regression`; review that inventory before relying on it. `migration run`
resolves a separate hash-pinned lock for each side and dependency lane, reuses those locks on later
runs, prepares the isolated environments, verifies every local editable install and both exact
released versions, and writes one data-safe JSON report per lane. Pass `--refresh-locks` only to
deliberately update dependency selection. Every local target reports a path-free Git/content
identity that is bound into findings and replay, so each local source must be a Git worktree with a
committed HEAD. Local/local runs additionally take paired driver snapshots throughout execution,
fail if either checkout changes, and write a two-source `source-provenance.json`; mixed runs do not
write that paired report. Target environments need the package, its adapter dependencies and
PyArrow, not Parity; use `--reference-python` and `--candidate-python` for different Python 3.8+
runtimes. Parity never creates, switches or modifies worktrees. For automatic replay, the workspace
directory and its managed environments must be contained by the directory holding `parity.toml`;
managed initialization rejects other layouts, and the default `migrations/` layout colocates them.
See the
[user guide](docs/USER_GUIDE.md#managed-migration-workspaces).

`migration init`, `migration validate`, `migration run` and `replay` accept boolean `--json` and
emit exactly one versioned, data-safe document to stdout while preserving exit codes `0`/`1`/`2`.
Suggested commands are argv arrays, not shell strings. Discover every authored/output contract with
`parity schema list` and print one Draft 2020-12 schema with, for example,
`parity schema workspace`, `parity schema finding` or `parity schema agent-result`. Published schema
bytes are frozen with their contract version and do not depend on the installed Pydantic release.

Migrations are one active adjacent pair. Keep reusable controls/core cases, replace
transition-specific cases, and advance after promoting the candidate:

```bash
parity migration advance --reference-package "$NEXT_REFERENCE_PACKAGE_SPEC"
parity migration run
```

`advance` changes only the reference. For a released candidate, update `candidate_package` to the
next release or regenerate the reviewed workspace with `migration init --force` before running the
next pair. Historical transitions do not accumulate in the completion gate. See the
[user guide](docs/USER_GUIDE.md#managed-migration-workspaces) and
[migration completion protocol](docs/AGENT_MIGRATION.md).

## Other supported uses

The same contracts currently support dependency-version checks, large refactors, local versus
local Git comparisons, release regressions, alternative backends, Python rewrites and external
command implementations. Cross-language verification is available through the target protocol,
not a language-specific plugin. Capturing files, databases, HTTP calls and other effects is a
future contract extension; today those effects should be projected into an explicit return value
by a reviewed adapter. See [use cases and boundaries](docs/USE_CASES.md).

## Python and pytest

```python
from parity import check, verify

suite = check("parity.toml", cases={"orders"})
assert suite.passed

suite = verify(
    reference_orders,
    candidate_orders,
    fixture=sample,
    reference_adapter="pandas",
    candidate_adapter="polars",
    artifact_dir=".parity/live-orders",
)
assert suite.passed
```

The pytest plugin exposes the same assertion as a fixture:

```python
def test_orders_migration(parity):
    parity.check("parity.toml", cases={"orders"})
```

Python APIs return typed result models and never terminate the process. The CLI adds the stable
`0`/`1`/`2` exit contract.

## GitHub Actions

```yaml
permissions:
  contents: read

steps:
  - uses: actions/checkout@v4
  - uses: leighshepperson/parity@v0
    with:
      config: parity.toml
      upload-artifact: "false"
```

Reports are data-safe projections, but replay artifacts contain input values. Upload them only
under an appropriate access and retention policy. Pin a reviewed full commit SHA when the Action
revision itself must be immutable. See the [Action guide](docs/GITHUB_ACTION.md).

## Boundaries

Parity answers: *did these executable contracts differ anywhere we looked?* It does not decide
which side expresses business intent, prove correctness outside the input domain, infer a complete
public API, justify a weakened policy, migrate code, or securely sandbox hostile targets.

Current comparison understands canonical frames, series, arrays, mappings, sequences, scalars,
returns, raises and input mutation. Broader effect capture is intentionally future work. Run unknown
code in a container or hardened runner.

## Documentation

- [User guide](docs/USER_GUIDE.md)
- [Use cases and current boundaries](docs/USE_CASES.md)
- [Configuration reference](docs/CONFIG_REFERENCE.md)
- [External target protocol](docs/TARGET_PROTOCOL.md)
- [Architecture and artifact contracts](docs/ARCHITECTURE.md)
- [Migration completion protocol](docs/AGENT_MIGRATION.md)
- [Security and privacy](docs/SECURITY.md) and [threat model](docs/THREAT_MODEL.md)
- [Pytest integration](docs/PYTEST.md) and [GitHub Action](docs/GITHUB_ACTION.md)
- [External validation log](case_studies/ADOPTION_LOG.md) and [fault corpus](docs/FAULT_CORPUS.md)
- [Roadmap](docs/ROADMAP.md), [development guide](docs/DEVELOPMENT.md) and
  [release notes](CHANGELOG.md)

## Development status

Parity is pre-1.0 and supports its latest minor release. Minor releases may simplify configuration,
artifacts and APIs; patch releases preserve their minor line's contracts.

Licensed under the [Apache License 2.0](LICENSE).
