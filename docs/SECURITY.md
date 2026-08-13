# Security and privacy

Parity is local-first. The released core does not require an account, make telemetry calls, upload
source code or send inputs to an AI provider. Verification runs where the user invokes it.

That property reduces exposure; it does not make every output non-sensitive.

## Data inventory

| Material | May contain input values | Default location | Intended audience |
|---|---:|---|---|
| Terminal/Markdown/JSON/JUnit report | No dataframe/value payloads | Console or requested path | Developers and CI |
| `input.arrow` or bundled `input-*.arrow` / optional Parquet copies | Yes | Counterexample directory | Restricted engineering team |
| `manifest.json` | Metadata, paths and hashes | Counterexample directory | Restricted engineering team |
| `result.json` in artifact | Structured mismatch evidence | Counterexample directory | Restricted engineering team |
| `replay.json` | Command/config references | Counterexample directory | Restricted engineering team |
| `parity doctor --json` | Executable, platform and working-directory paths | Console | Support after review |

Even a minimized synthetic input can reveal a category, boundary or example copied from a schema.
Treat the entire artifact directory at the same classification as its source fixture.

## Safe operating pattern

1. Use synthetic fixtures where they represent the contract adequately.
2. If production-shaped fixtures are necessary, remove direct identifiers and rare values before
   committing or uploading them.
3. Keep `.parity/` ignored by Git; the repository `.gitignore` does this by default.
4. Restrict CI artifact readers and set the shortest useful retention period.
5. Do not paste counterexamples into public issues. Recreate a synthetic witness.
6. Keep callable wrappers pure and offline. Inject no credentials unless unavoidable.
7. Run untrusted implementations inside a hardened container/VM with no credentials or network.
8. Pin Parity and dependency versions for release gates; verify published package provenance.

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

## Supply chain

Releases are built in GitHub Actions, checked with `twine`, attested and published through PyPI
trusted publishing. CI always runs dependency audit. Dependency review and CodeQL run for public
repositories, or for a private repository when GitHub Advanced Security is enabled and the
`ENABLE_GHAS` repository variable is set to `true`. Dependabot covers Python and Actions
dependencies. Consumers with stronger requirements should pin hashes or mirror packages through
their approved registry.

The composite Action installs the action's own source revision by default. A caller may request a
strict `parity-version`; arbitrary package specifiers are rejected.

## Executed code

Reference and candidate callables are arbitrary Python. Separate worker processes provide timeout,
crash and cross-implementation state isolation but can still access the filesystem, network and
inherited environment permitted to the invoking user. A configured campaign reuses each side's
worker, so module state and spawned activity may persist between examples until campaign teardown.
Workers can consume resources, spawn children or exploit native dependencies.

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
