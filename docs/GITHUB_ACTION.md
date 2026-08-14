# GitHub Action

The composite action installs the same Parity source revision as the action tag, runs configured
cases, writes JSON/JUnit/Markdown reports, appends Markdown to the job summary, optionally uploads
evidence and then enforces Parity's exit code.

```yaml
name: Semantic migration
on: [pull_request]

permissions:
  contents: read

jobs:
  parity:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install project dependencies
        run: python -m pip install -e .
      - id: parity
        uses: leighshepperson/parity@v0
        with:
          config: migrations/parity.toml
          cases: orders,customers
          tags: critical
          max-examples: "500"
          max-findings: "3"
          stability-repeats: "2"
          performance: "false"
          artifact-path: .parity
          upload-artifact: "false"
```

Case and tag filters are combined by the CLI.

## Release selection

`leighshepperson/parity@v0` is the moving channel for stable 0.x releases that preserve the
composite Action interface. It advances only after the corresponding package has passed the release
suite and published successfully. It never intentionally points at a prerelease or an older
release. Do not use `@main` for CI.

A tag can be moved. For immutable execution, replace `v0` with a reviewed full-length commit SHA
from this repository and use dependency automation to review later updates. The optional
`parity-version` input strictly pins a PyPI release instead of installing the Action's source
revision; leave it empty unless deliberately testing a different package release.

## Migration-manifest gate

The composite Action runs ordinary Parity cases and does not accept a migration manifest. Run the
declared-inventory gate as a separate CLI step after installing the project and Parity:

```yaml
- name: Gate declared migration surface
  run: >-
    parity migration check
    --manifest migrations/migration.toml
    --config migrations/parity.toml
    --json .parity/migration-status.json
```

Do not pass case, tag, search-budget or performance overrides to the completion gate. Use the
ordinary Action or `parity check` for focused iteration. The migration JSON report omits compared
values, but mapped cases may create sensitive replay artifacts under `.parity/`; retain the Action's
opt-in upload policy for that directory.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `config` | `parity.toml` | Configuration path in the checked-out repository. |
| `cases` | empty | Comma-separated case names. |
| `tags` | empty | Comma-separated tags. |
| `max-examples` | empty | Override generation budget. |
| `max-findings` | empty | Override the maximum distinct mismatch signatures per case. |
| `stability-repeats` | empty | Override same-input observations per implementation, from 1 through 10. |
| `performance` | `true` | `true` or `false`. |
| `python-version` | `3.12` | setup-python interpreter. |
| `parity-version` | empty | Optional strict package version. |
| `artifact-path` | `.parity` | Uploaded evidence path. |
| `upload-artifact` | `false` | Opt in to uploading reports and raw counterexamples on pass, mismatch or error. |
| `artifact-name` | `parity-report` | GitHub artifact name. |
| `retention-days` | `14` | Artifact lifetime. |

Outputs are `exit-code`, `result-json` and `junit-xml`. A failing action step prevents normal later
steps; use `if: always()` or job-level handling if a later consumer needs them.

## Dependency environments

The action installs Parity, pandas, Polars and its own runtime dependencies. Install your project
and any private packages in earlier steps. Callable `python` fields can point to separately prepared
legacy/candidate virtual environments.

## Artifact privacy

JSON, JUnit, Markdown and step-summary reports omit dataframe values. Counterexample directories
contain the actual minimized input as Arrow and, when representable, Parquet. When a generated
schema was inferred from a real fixture, those files may reproduce fixture values. Use private repositories, constrained
artifact permissions and an appropriate retention period. Upload is disabled by default; set
`upload-artifact: "true"` only when policy permits remote evidence storage.

Do not place credentials in the configuration or action inputs. GitHub masks known secrets in logs,
but Parity does not attempt to discover or redact arbitrary secrets embedded in dataframe values.
