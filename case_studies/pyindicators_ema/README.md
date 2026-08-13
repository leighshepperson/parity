# PyIndicators pandas/Polars EMA study

This study calls the public `pyindicators.ema` implementation on both supported dataframe
backends at tag `v0.22.0`, commit
[`9aec2b2caa502301bca6e9937e89e57f8ddeefe1`](https://github.com/coding-kitties/PyIndicators/commit/9aec2b2caa502301bca6e9937e89e57f8ddeefe1).
Both fixtures are small synthetic datasets and contain no external or user data.

The finite control passes. With a null after the first observation, the pandas branch returns an
EMA while the pinned manual Polars loop raises `TypeError`. Parity therefore records a returned-
versus-raised semantic finding. This is evidence of backend divergence in the pinned code, not a
claim that one backend is correct or that the upstream project has a bug.

The upstream commit reports distribution version `0.21.0` in its package metadata even though the
Git tag is `v0.22.0`; the captured provenance faithfully shows the distribution metadata. The
requirements file locks the complete resolved study environment. In particular, its scipy and
scikit-learn versions are resolved study pins, not versions declared exactly by upstream. The
environment uses patched `pyarrow==23.0.1`.

## Reproduce

From the Parity repository root with CPython 3.11 or later:

```sh
python -m venv /tmp/parity-pyindicators-v022
PIP_CACHE_DIR=/tmp/parity-pip-cache \
  /tmp/parity-pyindicators-v022/bin/pip install \
  -r case_studies/pyindicators_ema/environments/requirements.txt
/tmp/parity-pyindicators-v022/bin/pip install --no-deps -e .
(
  cd case_studies/pyindicators_ema
  /tmp/parity-pyindicators-v022/bin/python direct_repro.py \
    > reports/direct-repro.json
  /tmp/parity-pyindicators-v022/bin/parity check --config parity.toml \
    --no-performance --json reports/report.json --markdown reports/report.md
)
```

Expected result: `ema-finite-control` passes, `ema-nullable-backend-divergence` fails with a
returned-versus-raised finding, and `parity check` exits `1`. Compared row values are omitted from
the reports. Replay artifacts stay in ignored `.parity-pyindicators/`.
