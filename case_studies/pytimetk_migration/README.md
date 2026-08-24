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

## Recommended: managed reference and candidate environments

The public wrappers in `pytimetk_pilot.py` are intentionally thin. Reference workers call the
stock public API with `engine="pandas"`; candidate workers call the same public API with
`engine="polars"`. The repaired source patch is installed only in candidate workers. Each backend
is exercised in two dependency lanes:

| Lane | NumPy | pandas | Polars | PyArrow | Source |
|---|---:|---:|---:|---:|---|
| release | 2.0.2 | 2.2.3 | 1.21.0 | 16.1.0 | PyTimeTK 2.5.1 lock |
| current | 2.5.2 | 3.0.5 | 1.43.2 | 25.0.1 | pinned 2026-08-14 |

Parity hides the resulting four worker environments behind one controller installation, one
repaired candidate checkout and one command. Install Parity in the controller environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install parity-check
```

Parity deliberately does not fetch or change project source. Prepare the one declared candidate
checkout explicitly:

```bash
git clone --branch v2.5.1 --depth 1 \
  https://github.com/business-science/pytimetk.git ../pytimetk-migration-candidate
git -C ../pytimetk-migration-candidate apply ../pytimetk_migration/patches/candidate.patch
git -C ../pytimetk-migration-candidate apply ../pytimetk_migration/patches/local-version.patch
```

The semantic patch remains separately upstreamable. The small local-version patch identifies only
the repaired checkout as `pytimetk==2.5.1+parity.1`, so the fail-closed runtime requirements cannot
confuse it with the stock `pytimetk==2.5.1` reference installed from the package index.

Regenerate the synthetic fixtures, verify the declarations and run both lanes:

```bash
python make_fixtures.py
pytest -q ../../tests/test_pytimetk_migration_case_study.py
parity migration run --workspace parity.workspace.toml
```

`parity.workspace.toml` is the complete human-facing environment declaration. It points at the
worker-path-free `parity.workspace-config.toml`, the shared `parity.cases.toml`, the migration
ledger, the sibling candidate checkout and the two reviewed lane inputs. Keeping the checkout
outside the harness prevents it from shadowing the released reference import. Parity resolves
hash-pinned locks, prepares and reuses the paired tox environments through `tox-uv`, overrides
worker interpreters in memory, then writes private reports to
`.parity/workspace/reports/{release,current}.json`. Pass `--refresh-locks` only when intentionally
refreshing the dependency resolution.

The expected managed result is two passing lane reports: five units pass, six are explicitly
excluded, none fail, error or remain uncovered, and all 15 nested campaigns pass in each lane.

## Evidence and acceptance

The repaired acceptance state is five covered units passed, six explicit exclusions, no failed,
errored or uncovered unit, and all 15 campaigns passed in both dependency lanes. The separate
`parity.version-drift.toml` corpus contains ten representative release-to-current dependency checks;
all ten pass.

Stock PyTimeTK 2.5.1 failed all five covered units without an execution error. The retained report
files document those mismatch shapes, while small synthetic witnesses live in `fixtures/` and
`upstream_tests/`. Raw `.parity` counterexamples remain private and ignored. Do not manufacture a
baseline report from `FINDINGS.md`; capture it from an actual stock run when revalidating the study.

See [FINDINGS.md](FINDINGS.md) for the evidence register and [VALIDATION.md](VALIDATION.md) for the
completion checklist.
