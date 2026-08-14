# PyTimeTK five-API migration pilot

This study uses Parity's migration ledger to audit and repair the pandas-to-Polars backend boundary
of five public APIs in PyTimeTK 2.5.1:

- `augment_lags`
- `augment_rolling`
- `augment_ewm`
- `augment_macd`
- `pad_by_time`

The declared in-scope contract contains 15 campaigns: a finite generated control, grouped or
ordering behavior, and a nullable hostile fixture for each API. Six deliberately excluded units in
`migration.toml` keep CUDA, memory downcasting, custom execution, selector parsing and advanced
calendar semantics visible without turning this bounded pilot into a rewrite of all PyTimeTK.

Passing the gate means all five **declared CPU-core units** passed. It does not mean every PyTimeTK
API or every option of these functions was migrated.

## Recommended: one managed workspace

The public wrappers in `pytimetk_pilot.py` are intentionally thin. Reference workers call the
stock public API with `engine="pandas"`; candidate workers call the same public API with
`engine="polars"`. The repaired source patch is installed only in candidate workers. Each backend
is exercised in two dependency lanes:

| Lane | NumPy | pandas | Polars | PyArrow | Source |
|---|---:|---:|---:|---:|---|
| release | 2.0.2 | 2.2.3 | 1.21.0 | 16.1.0 | PyTimeTK 2.5.1 lock |
| current | 2.5.2 | 3.0.5 | 1.43.2 | 25.0.1 | pinned 2026-08-14 |

The managed workspace hides the resulting four worker environments behind one controller
installation, one repaired candidate checkout and one command. Install the workspace extra in the
environment from which you run Parity:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install "parity-check[workspace]"
```

Parity deliberately does not fetch or change project source. Prepare the one declared candidate
checkout explicitly:

```bash
git clone --branch v2.5.1 --depth 1 \
  https://github.com/business-science/pytimetk.git candidate-src/pytimetk
git -C candidate-src/pytimetk apply ../../patches/candidate.patch
git -C candidate-src/pytimetk apply ../../patches/local-version.patch
```

The semantic patch remains separately upstreamable. The small local-version patch identifies only
the repaired checkout as `pytimetk==2.5.1+parity.1`, so the fail-closed runtime requirements cannot
confuse it with the stock `pytimetk==2.5.1` reference installed from the package index.

Regenerate the synthetic fixtures, verify the declarations and run both lanes:

```bash
python make_fixtures.py
pytest -q ../../tests/test_pytimetk_migration_case_study.py
parity migration run
```

`parity.workspace.toml` is the complete human-facing environment declaration. It points at the
worker-path-free `parity.workspace-config.toml`, the shared `parity.cases.toml`, the migration
ledger, the candidate checkout and the two reviewed lane inputs. Parity resolves hash-pinned locks,
prepares and reuses the paired tox environments through `tox-uv`, overrides worker interpreters in
memory, then writes private reports to `.parity/workspace/reports/{release,current}.json`. Pass
`--refresh-locks` only when intentionally refreshing the dependency resolution.

The expected managed result is two passing lane reports: five units pass, six are explicitly
excluded, none fail, error or remain uncovered, and all 15 nested campaigns pass in each lane.

## Historical/manual four-environment reproduction

The checked-in reports predate the managed workspace. The following lower-level process remains a
useful exact reproduction path and supports the separate direct version-drift check. It creates
four source checkouts and exposes all worker paths; new pilots should prefer the managed flow above.

Run these commands from this directory. `uv pip compile` captures the complete transitive graph;
the checked-in `requirements.in` files preserve the historical direct constraints.

```bash
for lane in release current; do
  uv pip compile "environments/$lane/requirements.in" \
    --output-file "environments/$lane/requirements.txt"

  for endpoint in reference candidate; do
    uv venv --python 3.12 "environments/$lane/$endpoint/.venv"
    uv pip sync \
      --python "environments/$lane/$endpoint/.venv/bin/python" \
      "environments/$lane/requirements.txt"

    git clone --branch v2.5.1 --depth 1 \
      https://github.com/business-science/pytimetk.git \
      "environments/$lane/$endpoint/src/pytimetk"
  done

  git -C "environments/$lane/candidate/src/pytimetk" \
    apply "../../../../../patches/candidate.patch"
  git -C "environments/$lane/candidate/src/pytimetk" \
    apply "../../../../../patches/local-version.patch"

  for endpoint in reference candidate; do
    uv pip install --reinstall --no-deps \
      --python "environments/$lane/$endpoint/.venv/bin/python" \
      -e "environments/$lane/$endpoint/src/pytimetk"
  done
done
```

The explicit environments now point at clean checkouts of the same `v2.5.1` commit. Run the
complete, unfiltered ledger in each lane and then the direct dependency-version comparisons:

```bash
environments/release/reference/.venv/bin/parity doctor --config parity.release.toml
environments/release/reference/.venv/bin/parity migration check \
  --manifest migration.toml \
  --config parity.release.toml \
  --json reports/final/release/migration.json

environments/current/reference/.venv/bin/parity doctor --config parity.current.toml
environments/current/reference/.venv/bin/parity migration check \
  --manifest migration.toml \
  --config parity.current.toml \
  --json reports/final/current/migration.json

environments/current/reference/.venv/bin/parity check \
  --config parity.version-drift.toml \
  --json reports/version-drift/report.json
```

`parity.version-drift.toml` adds ten direct comparisons: one representative case per API for stock
pandas release-to-current and repaired Polars release-to-current. This catches dependency drift
that two independent backend comparisons could otherwise miss.

The repaired acceptance state is:

- both migration commands exit `0`;
- five units pass, six units are explicitly excluded, and none fail, error or remain uncovered;
- all 15 nested campaigns pass in both lanes; and
- all ten direct version-drift cases pass.

The checked-in 2026-08-14 evidence meets that state. Stock 2.5.1 failed all five covered units in
both lanes without an execution error; the source repair then passed all 15 cases in both lanes,
and the ten direct dependency-version checks passed. Every one of the 15 private stock artifacts
referenced by the baseline reports was replayed before the patched candidates were restored. The
four generated controls each exercised 100 inputs in the repaired runs; the eleven reviewed
fixture cases each exercised exactly one input and performed no inferred-domain search.

Before applying `patches/candidate.patch`, stock PyTimeTK is expected to keep the gate red. Capture
that live run under `reports/baseline/`; do not manufacture a baseline report from the findings
table. Private `.parity-*` counterexamples are intentionally ignored. Promote only small,
synthetic witnesses into `fixtures/` and `upstream_tests/`.

See [FINDINGS.md](FINDINGS.md) for the evidence register and [VALIDATION.md](VALIDATION.md) for the
completion checklist.
