# pandas cross-version categorical group-by study

This study runs one unchanged callable in two explicitly supplied Python environments: pandas
`2.3.3` on the reference side and `3.0.5` on the candidate side. The callable constructs the same
two-value categorical domain, groups data containing only one value, and intentionally omits the
`observed` argument.

pandas 2.3.3 retains the historical `observed=False` default and emits an unused-category row;
pandas 3 uses `observed=True` by default and emits only observed categories. Parity records the
resulting shape difference as intentional version drift, not a regression claim. Version `3.0.5`
is used instead of the researched `3.0.4` because the latter was yanked from PyPI for reported
datetime-related segfaults. Both fixtures are synthetic and both sides use the same import target.

## Reproduce

From the Parity repository root with CPython 3.11 or later:

```sh
for side in reference candidate; do
  python -m venv case_studies/pandas_version_groupby/environments/$side/.venv
  PIP_CACHE_DIR=/tmp/parity-pip-cache \
    case_studies/pandas_version_groupby/environments/$side/.venv/bin/pip install \
    -r case_studies/pandas_version_groupby/environments/$side/requirements.txt
  case_studies/pandas_version_groupby/environments/$side/.venv/bin/pip install \
    --no-deps -e .
done
(
  cd case_studies/pandas_version_groupby
  environments/reference/.venv/bin/python direct_repro.py \
    > reports/reference-direct-repro.json
  environments/candidate/.venv/bin/python direct_repro.py \
    > reports/candidate-direct-repro.json
  set +e
  environments/candidate/.venv/bin/parity check --config parity.toml \
    --no-performance --json reports/report.json --markdown reports/report.md
  test $? -eq 1
)
```

Expected result: one shape finding and exit code `1`. The committed report captures different
pandas versions and the same Python, NumPy, and PyArrow versions for both workers. Compared row
values are omitted. Replay artifacts stay in ignored `.parity-pandas-versions/`.
