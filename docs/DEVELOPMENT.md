# Development guide

## Environment

```bash
git clone https://github.com/leighshepperson/parity.git
cd parity
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the same gates as CI:

```bash
ruff check .
ruff format --check .
mypy src/parity
python -m coverage run -m pytest
python -m coverage report
python -m build
```

Coverage starts before pytest so the installed Parity plugin's own import path is measured rather
than being incorrectly reported as uncovered initialization code.

Branch coverage is gated at the current measured baseline (75%). Treat the threshold as a
ratchet: new work should add focused tests and raise it when the sustained suite result reaches the
next whole percentage point. Do not lower it to land untested behavior.

During iteration:

```bash
pytest tests/test_comparison.py -q
pytest -k generation --maxfail=1
ruff check src/parity tests
```

## Dependency compatibility

Pull requests run the suite both across every supported Python version and against the lowest
direct-dependency set currently validated on Python 3.11. The exact direct floor is recorded in
`.github/constraints/minimum-py311.txt`; update that file deliberately when a lower bound changes,
and verify the complete suite before claiming the new bound.

The `Dependency drift` workflow checks pandas, Polars and PyArrow independently. Stable releases
run weekly from the validated direct floor, advancing one package at a time. Transitive packages
are resolved at install time, so their versions should also be captured from the failing job.
Prereleases run monthly and
are non-blocking early warnings. Both jobs skip performance tests and extended campaigns, so a
failure identifies a semantic or compatibility change rather than machine-speed noise.

Run the direct floor locally with Python 3.11:

```bash
python -m pip install --only-binary=:all: \
  --requirement .github/constraints/minimum-py311.txt
python -m pip install --no-deps --editable .
python -m pip check
pytest -m "not slow" --ignore=tests/test_performance.py
```

When a drift lane fails, reproduce it by changing only the named dependency from the direct
floor. Record the Python, NumPy, pandas, Polars and PyArrow versions before deciding whether the
result is a Parity regression, an adapter/interoperability issue or an upstream behaviour change.
Do not collapse null and NaN differences globally to make a matrix green; encode any accepted semantic
change in an explicit comparison policy and a focused regression test.

## Repository map

```text
src/parity/
  models.py          stable config/result contracts
  config.py          strict TOML loading and path resolution
  adapters/          pandas/Polars/Arrow boundary
  schema.py          portable schema inference/materialisation
  generation.py      deterministic cases and Hypothesis strategies
  execution.py       target sessions, protocol validation and observations
  portable_worker.py dependency-light Python target adapter (Python 3.8-compatible)
  provenance.py      bounded runtime identities and safe config fingerprints
  comparison.py      semantic equivalence policies
  engine.py          suite/live orchestration and replay
  artifacts.py       atomic evidence persistence
  reporting.py       redacted terminal/JSON/Markdown/JUnit views
  performance.py     interleaved benchmarks and gates
  diagnostics.py     evidence-based explanations
  cli.py             Typer commands
  pytest_plugin.py   pytest assertion facade
examples/            original synthetic fault corpus
tests/               unit, property and integration tests
```

## Engineering rules

- Preserve the canonical `Invocation` boundary and Arrow frame leaves. Do not add pairwise
  pandas/engine conversions.
- Keep the portable target worker independent of Parity and controller-only dependencies.
- Keep external commands and Python workers on the same versioned observation protocol.
- Make equivalence policy explicit and validated. Avoid “smart” implicit tolerance widening.
- An uncertain execution is an error, never a pass.
- Compare semantic success before measuring performance.
- Keep result models JSON serializable and reports value-redacted.
- Store a minimized counterexample losslessly in Arrow and add Parquet when its format supports the
  schema.
- Keep deterministic cases fast; mark genuinely extended tests `slow`.
- Use public APIs and synthetic examples only. Follow [clean-room provenance](CLEAN_ROOM.md).

## Adding an adapter

An adapter must round-trip supported frames through Arrow, preserve empty-frame schema, handle nulls
and temporal metadata, and describe unsupported types with a deterministic error. Add cross-adapter
property tests and at least one corpus fault that would be missed by naive conversion.

Do not make an engine a required dependency until its adapter is part of the supported core. Future
heavy engines should generally be optional extras and execute out of process.

## Changing contracts

Configuration and artifact changes have a larger blast radius than internal refactors. For any
change to models or manifests:

1. Define the complete current serialization contract in focused tests.
2. Change the contract version when a reader must distinguish the new shape.
3. Reject unsupported versions with an explicit error.
4. Update the config reference, architecture and Action integration.
5. Record the user-visible change in the release notes.

Never silently reinterpret a policy value.

## Test layers

- **Unit:** canonicalisation, comparator branches, config validation and report redaction.
- **Property:** adapter round trips, row-multiset comparison and schema generation invariants.
- **Target protocol:** preflight, timeout, crash, exception, mutation and environment boundaries.
- **Artifact:** atomic writes, hash binding, corrupt/incomplete replay rejection.
- **Corpus:** every deliberately wrong migration is detected and every corrected pair passes.
- **Integration:** CLI exit codes, pytest fixture and composite-action smoke path.

Tests must avoid network access, wall-clock flakiness and private fixtures. Statistical performance
logic uses controlled observations, while an end-to-end CPU-heavy reference/candidate test proves
that target timing is actually measured. Neither should assert a machine-specific absolute speed.

## Documentation and release

Examples in documentation should parse or run in tests where practical. Release tags are `vX.Y.Z`
and must exactly match the package version. The protected release workflow independently reruns
lint, formatting, strict typing and the coverage suite; it then builds once, checks distributions,
attests them and publishes through PyPI trusted publishing. After publication succeeds, the release
workflow advances the matching Action major tag through `scripts/promote_action_major.py`. The
promoter accepts only final releases, verifies the source version, rejects rollback or rewritten
release identity and uses a force-with-lease against the observed remote tag.

The separate `workflow_dispatch` entry point exists only for a dry-run or deliberate recovery
against an immutable release tag; it is not a second normal release path. The automated release
job moves the Action channel only after publication succeeds. Leave `dry_run` enabled while
reviewing the validated commit, and disable it only for the intended recovery. The same monotonic
and lease guards apply; do not create or force-move the alias by hand.

Before a managed-environment feature is published, build its wheel, install that wheel into a
clean controller environment, and run the integration smoke from there:

```bash
PARITY_SMOKE_DIR="$(mktemp -d)"
python -m build --wheel --outdir "$PARITY_SMOKE_DIR/dist"
python -m venv "$PARITY_SMOKE_DIR/controller"
"$PARITY_SMOKE_DIR/controller/bin/python" -m pip install \
  "$PARITY_SMOKE_DIR"/dist/parity_check-*.whl
"$PARITY_SMOKE_DIR/controller/bin/parity" --version
"$PARITY_SMOKE_DIR/controller/bin/parity" migration run \
  --workspace path/to/parity.workspace.toml
```

The fresh output directory guarantees that the wheel glob selects only the build under test. Target
locks must contain the portable worker's transport requirement and the selected
application/adapter dependencies, but must not install the full controller merely to satisfy the
harness. Do not add a local-wheel override to user-facing workspace files; published users install
the ordinary package release.

For contribution mechanics, see [CONTRIBUTING.md](CONTRIBUTING.md).
