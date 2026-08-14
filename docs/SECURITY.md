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
| Workspace locks and generated tox config | Package versions/hashes and local source/interpreter paths | `.parity/workspace/` | Developers and CI |
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
10. Review the candidate checkout and package indexes before a managed workspace installs from
    either source; use an approved mirror or offline cache where policy requires it.

## Secrets

Parity neither needs nor manages secrets. The `CallableSpec.environment` field is a literal mapping
and will be stored in configuration; do not use it for credentials. In CI, inherit approved secrets
from the runner only for wrappers that need them, and ensure neither callable returns or logs them.

Parity does not scrape environment variables into reports. Python code executed by Parity has the
same effective access as its worker process and can read inherited credentials. Isolation is not a
permission boundary.

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

Evidence-verification reports apply the same data-safe projection. The `ms1:...` value binds a
stable mismatch-shape classification, not source identity: despite the word “signature” in the
model, it is not a digital signature, MAC, package attestation or authorization decision. Artifact
manifest hashes detect local file changes, but a party able to replace the artifact can replace its
hash manifest too. Use signed release provenance or an external attestation system when evidence
crosses trust domains.

## Supply chain

Releases are built in GitHub Actions, checked with `twine`, attested and published through PyPI
trusted publishing. CI always runs dependency audit. Dependency review and CodeQL run for public
repositories, or for a private repository when GitHub Advanced Security is enabled and the
`ENABLE_GHAS` repository variable is set to `true`. Dependabot covers Python and Actions
dependencies. Consumers with stronger requirements should pin hashes or mirror packages through
their approved registry.

The optional migration workspace asks uv to resolve hash-pinned requirements locks and asks tox
with tox-uv to create isolated workers. Resolution and installation may access configured package
indexes and their normal caches. The exact reference requirement, lane requirement files, candidate
packaging metadata and every resolved dependency are supply-chain inputs. Use a trusted index,
review lock changes and keep `.parity/workspace` private because generated configuration can contain
local paths. Managed setup rejects a workspace/import layout that exposes the editable candidate
checkout to the reference worker: otherwise a flat-layout package could shadow the installed
reference while package metadata still appeared correct. Keep the workspace and wrappers in a
separate `migrations/` directory and do not add the candidate root to worker `PYTHONPATH`.

When uv or tox fails, Parity captures their raw stdout and stderr under
`.parity/workspace/logs/` while keeping it out of the data-safe terminal error. Those private logs
can contain index URLs, credentials, paths, or packaging output. Do not upload or publish them.

Parity treats tox, tox-uv and uv as environment-lifecycle details. It does not clone repositories,
select branches or commits, apply patches, or edit the candidate checkout. That boundary prevents
environment setup from silently choosing the source being evaluated; it does not make an existing
checkout or its build backend safe.

The composite Action always installs the Action's selected source revision. Public examples use the
moving `v0` tag, which tracks the latest final 0.x release after its matching package publishes
successfully. Minor releases may break public contracts before 1.0. Pin a reviewed full-length
commit SHA when an immutable Action and package revision is required, and do not run the default
branch directly in CI.

## Executed code

Reference and candidate callables are arbitrary Python. Separate worker processes provide timeout,
crash and cross-implementation state isolation but can still access the filesystem, network and
inherited environment permitted to the invoking user. A configured campaign reuses each side's
worker, so module state and spawned activity may persist between examples until campaign teardown.
Workers can consume resources, spawn children or exploit native dependencies.

`parity migration check` executes the union of cases named by the manifest under the same worker
model. Manifest unit IDs and exclusion reasons are descriptive only and are never imported or
evaluated. Unknown case names are rejected before a worker starts.

`parity migration run` additionally resolves and installs packages before executing the same union
in every dependency lane. `parity evidence verify` checks local artifact integrity and replays every
report-referenced finding. Both commands must be run only against trusted source and packaging
metadata, or inside the hardened environment described below.

Configured runs and artifact replay execute the selected Python interpreter path. A project-local
virtual-environment entry point may be a symlink to a host Python binary; Parity preserves that
entry point because its surrounding environment determines installed packages. Replay requires the
recorded path to stay lexically within the invocation project, but this is provenance hygiene, not
a sandbox or trust boundary. Review repository code and interpreter paths before replaying evidence
from another source. Fixture, manifest and workdir containment checks continue to resolve symlinks
and reject escapes.

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
