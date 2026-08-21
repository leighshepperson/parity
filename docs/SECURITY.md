# Security and privacy

Parity is local-first. The released core does not require an account, make telemetry calls, upload
source code or send inputs to an AI provider. Verification runs where the user invokes it.

That property reduces exposure; it does not make every output non-sensitive.

## Data inventory

| Material | May contain input values | Default location | Intended audience |
|---|---:|---|---|
| Terminal/Markdown/JSON/JUnit report | No dataframe/value payloads | Console or requested path | Developers and CI |
| Migration manifest | No dataframe/value payloads; contains unit IDs and exclusion reasons | User-selected repository path | Developers and reviewers |
| Migration JSON/terminal report | No dataframe/value payloads; contains redacted inventory metadata | Console or requested path | Developers and CI |
| Evidence-verification JSON | No dataframe/value payloads; contains redacted case/artifact labels and mismatch digests | User-selected path | Developers and CI |
| Workspace locks and generated environment config | Package versions/hashes and local source/interpreter paths | `.parity/workspace/` | Developers and CI |
| `input.arrow` or bundled `input-*.arrow` / optional Parquet copies | Yes | Counterexample directory | Restricted engineering team |
| `manifest.json` | Metadata, paths and hashes | Counterexample directory | Restricted engineering team |
| `result.json` in artifact | Structured mismatch evidence | Counterexample directory | Restricted engineering team |
| `replay.json` | Command/config references | Counterexample directory | Restricted engineering team |
| `parity doctor --json` | Executable, platform and working-directory paths | Console | Support after review |
| `parity doctor --config ... --json` | Path-free allowlisted runtime versions | Console | Developers and CI |

Even a minimized synthetic input can reveal a category, boundary or example copied from a schema.
Treat the entire artifact directory at the same classification as its source fixture.

## Safe operating pattern

1. Use synthetic fixtures where they represent the contract adequately.
2. If production-shaped fixtures are necessary, remove direct identifiers and rare values before
   committing or uploading them.
3. Keep `.parity/` ignored by Git. Parity creates a self-ignoring private-state root, but retain an
   explicit repository rule when other tools may replace or move that directory.
4. Restrict CI artifact readers and set the shortest useful retention period.
5. Do not paste counterexamples into public issues. Recreate a synthetic witness.
6. Keep callable wrappers pure and offline. Inject no credentials unless unavoidable.
7. Run untrusted implementations inside a hardened container/VM with no credentials or network.
8. Pin Parity and dependency versions for release gates; verify published package provenance.
9. Keep secrets, customer names, private paths and copied data out of migration unit IDs and
   exclusion reasons, even though report projections apply ordinary diagnostic redaction.
10. Review every local checkout and package index before a managed workspace installs either side;
    use an approved mirror or offline cache where policy requires it.

## Secrets

Parity neither needs nor manages secrets. The `CallableSpec.environment` field is a literal mapping
and will be stored in configuration; do not use it for credentials. In CI, inherit approved secrets
from the runner only for wrappers that need them, and ensure neither callable returns or logs them.

Parity does not scrape environment variables into reports. Python and command targets have the same
effective access as their process and can read inherited credentials. Isolation is not a permission
boundary.

Runtime provenance is allowlisted rather than discovered broadly. Reports may contain Python,
platform and installed-version strings for Parity's core dependencies plus distributions explicitly
named in `record_distributions`. They never contain environment values, executable paths, cwd,
hostnames, command lines or a complete installed-package inventory. Distribution metadata that
cannot be represented by the bounded safe contract is reported only as unavailable.

Migration reports contain redacted unit IDs, case names and exclusion reasons plus the ordinary
data-safe Parity case report. They omit compared values and do not reproduce callable targets or
configuration bodies. Redaction removes common absolute-path and secret-assignment forms; it is not
a general secret scanner. The manifest is project-controlled text and should itself be reviewed
before it is committed, uploaded or passed to an external AI system.

Evidence-verification reports apply the same data-safe projection. The `ms3:...` value binds a
stable mismatch-shape classification, not source identity: despite the word “signature” in the
model, it is not a digital signature, MAC, package attestation or authorization decision. Artifact
manifest hashes detect local file changes, but a party able to replace the artifact can replace its
hash manifest too. Use signed release provenance or an external attestation system when evidence
crosses trust domains.

Caught target exceptions are reduced to a qualified type, normalized message fingerprint and a
small allow-list of structured reason metadata. Raw messages and tracebacks are not placed in
reports. Normalization removes common paths, secrets, witness literals, addresses, timestamps, IDs
and version strings. Reports additionally restrict type names, validation codes and API subjects to
finite reviewed sets so identifier-shaped application data remains opaque. Private mismatch
artifacts retain exact evidence and should still be treated as sensitive test data.

FAILED reports expose only the safe projection needed to distinguish findings: Return/Raise state,
well-known qualified exception types, and allow-listed Pydantic error codes, location shapes and
NumPy API tokens when present. Custom identifiers remain `custom`; exact private evidence and the
data-free `ms3:` replay classifier keep findings independently inspectable and reproducible.

## Supply chain

Releases are built in GitHub Actions, checked with `twine`, attested and published through PyPI
trusted publishing. CI always runs dependency audit. Dependency review and CodeQL run for public
repositories, or for a private repository when GitHub Advanced Security is enabled and the
`ENABLE_GHAS` repository variable is set to `true`. Dependabot covers Python and Actions
dependencies. Consumers with stronger requirements should pin hashes or mirror packages through
their approved registry.

The optional migration workspace resolves hash-pinned requirements locks and creates isolated
target environments. Resolution and installation may access configured package indexes and their
normal caches. When `UV_CACHE_DIR` is unset, Parity keeps uv's cache in
`.parity/workspace/cache`; an explicit override is preserved for an approved shared or offline
cache. Either side's exact released requirement, each local checkout's packaging metadata, lane
requirement files and every resolved dependency are supply-chain inputs. Use a trusted index,
review lock changes and keep `.parity/workspace` private because generated configuration can contain
local paths. Managed setup rejects a workspace/import layout that exposes one editable checkout to
the wrong target: otherwise a flat-layout package could shadow the intended installation while
package metadata still appeared correct. Keep the workspace and wrappers in a separate
`migrations/` directory and do not add either checkout root to target `PYTHONPATH`.

When environment resolution or setup fails, Parity captures raw tool stdout and stderr under
`.parity/workspace/logs/` while keeping it out of the data-safe terminal error. Those private logs
can contain index URLs, credentials, paths, or packaging output. Do not upload or publish them.

Parity keeps environment creation and resolution behind its migration commands. It does not clone
repositories, select branches or commits, apply patches, or edit either checkout. That boundary
prevents setup from silently choosing the source being evaluated; it does not make an existing
checkout, target command or build backend safe.

The composite Action always installs the Action's selected source revision. Public examples use the
moving `v0` tag, which tracks the latest final 0.x release after its matching package publishes
successfully. Minor releases may break public contracts before 1.0. Pin a reviewed full-length
commit SHA when an immutable Action and package revision is required, and do not run the default
branch directly in CI.

## Executed code

Reference and candidate targets are arbitrary Python or protocol-speaking commands. Separate target
processes provide timeout, crash and cross-implementation state isolation but can still access the
filesystem, network and inherited environment permitted to the invoking user. A configured
campaign reuses each side's session, so module/process state and spawned activity may persist
between examples until campaign teardown. Targets can consume resources, spawn children or exploit
native dependencies.

The portable Python worker imports no Parity controller modules in the target environment. That
decouples dependencies; it does not reduce target code's authority. External command adapters have
the same trust model. Their strict Arrow/JSON protocol prevents accidental contract ambiguity, not
malicious access. See [the target protocol](TARGET_PROTOCOL.md).

A configured `generation.generator` is also arbitrary project Python, but it runs in the Parity
driver so it can construct a Hypothesis strategy. It is not protected by the target-process
boundary or invocation timeout. Run custom generators only from reviewed repositories; put the
entire Parity command in a container/VM for untrusted generator code. Generator output is bounded
by `max_examples`, but the factory itself can still allocate, block or access driver credentials.

`parity migration check` executes the union of cases named by the manifest under the same target
model. Manifest unit IDs and exclusion reasons are descriptive only and are never imported or
evaluated. Unknown case names are rejected before a target starts.

`parity migration run` additionally resolves and installs packages before executing the same union
in every dependency lane. `parity evidence verify` checks local artifact integrity and replays every
report-referenced finding. Both commands must be run only against trusted source and packaging
metadata, or inside the hardened environment described below.

Configured runs and artifact replay execute the selected Python interpreter or command path. A
configuration-local virtual-environment entry point may be a symlink to a host Python binary; Parity
preserves that entry point because its surrounding environment determines installed packages.
Configured replay paths are relative to the directory containing `parity.toml`; replay v2 derives
that base from the artifact's bounded ancestor declaration and never trusts the process current
directory. Paths must stay lexically within that configuration base, but this is
provenance hygiene, not a sandbox or trust boundary. External or missing paths leave bounded blocker
codes instead of host locations. A managed workspace, its generated environments and path-like
executables must remain inside the configured `parity.toml` directory for automatic replay;
wrappers import from the workspace directory. Review repository code and interpreter paths before
replaying evidence from another source. Fixture, manifest and workdir containment checks continue
to resolve symlinks and reject escapes.

For third-party or AI-produced code that has not been reviewed, put the entire Parity invocation in
a container/VM with:

- a read-only source mount and dedicated writable artifact directory;
- no cloud instance credentials or repository write token;
- network disabled unless explicitly needed;
- CPU, memory, process and wall-time limits; and
- a disposable user namespace.

## Reporting vulnerabilities

Use a private GitHub security advisory at
<https://github.com/leighshepperson/parity/security/advisories/new>. Include a synthetic reproduction,
affected version and impact. Do not attach sensitive artifacts. The latest released minor version is
the supported security line while Parity is pre-1.0.
