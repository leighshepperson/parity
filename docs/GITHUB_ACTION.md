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
          artifact-path: migrations/.parity
          upload-artifact: "false"
```

Case and tag filters are combined by the CLI.

## Release selection

`leighshepperson/parity@v0` is the moving channel for the latest final 0.x release. It advances only
after the corresponding package has passed the release suite and published successfully. Parity is
pre-1.0, so a minor release on this channel may change the Action interface or other public
contracts. Do not use `@main` for CI.

A tag can be moved. For immutable execution, replace `v0` with a reviewed full-length commit SHA
from this repository and use dependency automation to review later updates. The Action always
installs Parity from that selected revision, so its workflow and Python package cannot drift apart.

## Migration-manifest gate

The composite Action runs ordinary Parity cases and does not accept a migration manifest. Run the
declared-inventory gate as a separate CLI step after installing the project and Parity:

```yaml
- name: Gate declared migration surface
  run: >-
    parity migration check
    --manifest migrations/migration.toml
    --config migrations/parity.toml
    --json migrations/.parity/migration-status.json
- name: Upload declared-migration evidence
  if: ${{ always() }}
  uses: actions/upload-artifact@v4
  with:
    name: parity-migration-report
    path: migrations/.parity
    include-hidden-files: true
    retention-days: 14
```

Do not pass case, tag, search-budget or performance overrides to the completion gate. Use the
ordinary Action or `parity check` for focused iteration. The migration JSON report omits compared
values, but mapped cases may create sensitive replay artifacts under `migrations/.parity/`. The
composite Action cannot upload files created by a later, separate CLI step, so add a subsequent
`if: always()` upload only when policy permits remote evidence storage.

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
| `artifact-path` | `.parity` | Uploaded evidence path; set it to the config-relative artifact root when the config is nested. |
| `upload-artifact` | `false` | Opt in to uploading reports and raw counterexamples on pass, mismatch or error. |
| `artifact-name` | `parity-report` | GitHub artifact name. |
| `retention-days` | `14` | Artifact lifetime. |

The Action intentionally leaves case concurrency and target native-thread limits in the reviewed
configuration. Set top-level `jobs` and `native_threads` in `parity.toml`; use `jobs = 1` for
performance evidence intended as a gate or retained comparison.

Outputs are `exit-code`, `result-json` and `junit-xml`. A failing action step prevents normal later
steps; use `if: always()` or job-level handling if a later consumer needs them.

## Dependency environments

The action installs the Parity controller and its runtime dependencies. Install your project and
any private packages in earlier steps. Callable `python` fields can point to separately prepared
reference and candidate environments; each needs PyArrow, its selected adapter dependency and the
application, not a second full Parity installation. Command targets can point to a separately built
protocol adapter executable.

## Artifact privacy

JSON, JUnit, Markdown and step-summary reports omit dataframe values. Counterexample directories
contain the actual minimized invocation's frame leaves as Arrow and, when representable, Parquet.
When a generated
schema was inferred from a real fixture, those files may reproduce fixture values. Use private repositories, constrained
artifact permissions and an appropriate retention period. Upload is disabled by default; set
`upload-artifact: "true"` only when policy permits remote evidence storage.

Do not place credentials in the configuration or action inputs. GitHub masks known secrets in logs,
but Parity does not attempt to discover or redact arbitrary secrets embedded in dataframe values.
