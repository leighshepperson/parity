# skrub `AggJoiner` pandas/Polars case study

This reproducible study compares skrub's pandas and Polars aggregation paths at commit
`55dc7f45e140ccb76e768e3e4b4193f4eac3d5aa`. It uses only small synthetic fixtures and records
the `skrub` and `scikit-learn` distributions alongside Parity's core runtime provenance.

The study is intentionally narrow:

- four controls establish ordinary numeric, public `AggJoiner`, unique-mode, and Arrow-null
  behavior;
- two durable findings cover null grouping keys and tied modes;
- one version-sensitive finding covers native IEEE NaN aggregation.

Native migration contracts explicitly set `pandas_input = "native"`. The Arrow-null control
explicitly retains `pandas_input = "arrow"`. This prevents the dataframe materialization policy
from being an accidental, version-dependent part of the experiment.

The pandas tied-mode result contains a sequence-valued dataframe cell while Polars returns a
scalar string. Parity 0.2.0 preserves that heterogeneous output faithfully, so the configured
wrapper returns the public result without study-specific normalization.

## Dependency matrix

| Lane | Python | NumPy | pandas | Polars | PyArrow | scikit-learn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| floor | 3.11 | 1.26.0 | 2.1.0 | 1.5.0 | 16.0.0 | 1.4.2 |
| current | 3.12 | 2.5.2 | 3.0.5 | 1.43.2 | 25.0.1 | 1.9.0 |

The floor is the lowest matrix that passes Parity's full suite while remaining inside the pinned
skrub checkout's support ranges. In particular, it uses PyArrow 16.0.0, psutil 5.9.8, Typer
0.16.1, and Click 8.2.1 rather than package metadata's looser absolute lower bounds. SciPy 1.10.1
and Matplotlib 3.6.3 form a practical CPython 3.11 / NumPy 1.26 wheel set. The compiled
requirements files pin the transitive closure; the `.in` files show the intentional axes.

## Reproduce the current lane

From the repository root:

```sh
python3.12 -m venv .venv/skrub-current
.venv/skrub-current/bin/pip install \
  -r case_studies/skrub_agg_joiner/environments/current/requirements.txt
.venv/skrub-current/bin/pip install --no-deps -e .
.venv/skrub-current/bin/python case_studies/skrub_agg_joiner/make_fixtures.py
(
  cd case_studies/skrub_agg_joiner
  ../../.venv/skrub-current/bin/parity check --config parity.toml \
    --no-performance \
    --json reports/current/report.json \
    --markdown reports/current/report.md
  ../../.venv/skrub-current/bin/python direct_repro.py \
    > reports/current/direct-repro.txt
)
```

Parity exits 1 when it finds the expected semantic differences, so the report command is expected
to be non-zero. Inspect the report instead of treating that exit status as an infrastructure
failure.

## Reproduce the supported-floor lane

Use CPython 3.11 and the floor lock:

```sh
python3.11 -m venv .venv/skrub-floor
.venv/skrub-floor/bin/pip install \
  -r case_studies/skrub_agg_joiner/environments/floor/requirements.txt
.venv/skrub-floor/bin/pip install --no-deps -e .
.venv/skrub-floor/bin/python case_studies/skrub_agg_joiner/make_fixtures.py
(
  cd case_studies/skrub_agg_joiner
  ../../.venv/skrub-floor/bin/parity check --config parity.toml \
    --no-performance \
    --json reports/floor/report.json \
    --markdown reports/floor/report.md
  ../../.venv/skrub-floor/bin/python direct_repro.py \
    > reports/floor/direct-repro.txt
)
```

Failure replay artifacts are generated locally under `.parity-skrub/` and are not committed; their
manifests and endpoint provenance distinguish environments. The data-safe reports under `reports/`
are committed. `FINDINGS.md` defines the case classification. `VALIDATION.md` records the matrix
check. `UPSTREAM_ISSUES.md` contains drafts only and has not been filed.

## Target integrity

Both locks install skrub from the exact Git commit. `direct_repro.py` additionally checks the
SHA-256 of the loaded `_agg_joiner.py` against the pinned checkout before printing any finding.
