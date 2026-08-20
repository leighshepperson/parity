# Polars cross-version dynamic-window study

This study runs one unchanged callable in two explicitly supplied Python environments: Polars
`0.20.31` on the reference side and `1.41.1` on the candidate side. The callable invokes
`group_by_dynamic` with `closed="both"` and intentionally omits `offset`.

Polars 0.20.31 documented the default offset as negative `every`; Polars 1.41.1 documents it as
zero. At an exact boundary, the older runtime emits one preceding closed window and the newer
runtime does not. The resulting shape finding is intentional version drift, not a regression claim.
Both workers use the same target and synthetic fixture; only their interpreter paths differ.

The 0.20.31 target is deliberately older than the controller's `polars>=1.0` dependency floor.
Parity is not installed in either target environment: the dependency-light portable worker needs
only PyArrow, the selected adapter library and the study module. This is therefore a direct test of
the decoupled target protocol against that historical release. Both targets were live-executed
successfully with `pyarrow==23.0.1`.

## Reproduce

From the Parity repository root with CPython 3.11 or later:

```sh
python -m pip install -e ".[dev]"
for side in reference candidate; do
  python -m venv \
    case_studies/polars_version_dynamic/environments/$side/.venv
  PIP_CACHE_DIR=/tmp/parity-pip-cache \
    case_studies/polars_version_dynamic/environments/$side/.venv/bin/pip install \
    -r case_studies/polars_version_dynamic/environments/$side/requirements.txt
done
(
  cd case_studies/polars_version_dynamic
  environments/reference/.venv/bin/python direct_repro.py \
    > reports/reference-direct-repro.json
  environments/candidate/.venv/bin/python direct_repro.py \
    > reports/candidate-direct-repro.json
  set +e
  parity check --config parity.toml \
    --no-performance --json reports/report.json --markdown reports/report.md
  test $? -eq 1
)
```

Expected result: the case records one mismatch signature with shape and value counts, then exits
`1`. The committed report captures different Polars versions and the same Python, NumPy, and
PyArrow versions for both workers. It contains no compared row values. Replay artifacts stay in
ignored `.parity-polars-versions/`.
