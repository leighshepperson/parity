# Case study: pyjanitor `complete()` across pandas and Polars

On 14 August 2026, Parity 0.8.1 rechecked two data-preservation
contracts in pyjanitor's independently maintained pandas and Polars implementations
of `complete()`. The upstream checkout was pyjanitor's current `dev` head,
[`c1b57b9`](https://github.com/pyjanitor-devs/pyjanitor/tree/c1b57b993dca4348e9acc41301fe8526dcae57df),
which reports version `0.32.23`. GitHub identifies `dev` as the default branch, and
its Polars implementation has blob `b93ac672da43a47c5b6e4d832ecf333c0f3d5162`
at both `dev` and the pinned commit.

The study uses only public APIs and synthetic data. Neither implementation was
modified to produce the result.

## Result

- 2 deterministic fixtures and 2 paired comparisons (4 implementation calls)
- both findings reproduced with Parity 0.8.1 in 4.714 seconds
- both findings also reproduced without importing or invoking Parity
- the null-key probe found 4 differing rows
- the explicit-domain probe found 1 missing row and a shape difference

The confirmed findings are:

1. Existing rows with null completion keys retain their keys but lose their payload
   values in the Polars result.
2. Existing rows outside a supplied completion domain are omitted from the Polars
   result.

The [machine-readable report](report.json) captures schema-v3 provenance, including
pyjanitor `0.32.23`, pandas `3.0.5`, Polars `1.43.2`, PyArrow `25.0.1`, Python
`3.12.13`, and Parity `0.8.1`. The [redacted Markdown report](report.md) records the
mismatch classes without compared row values. Copy-ready, standalone upstream issue
reports are in [`UPSTREAM_ISSUES.md`](UPSTREAM_ISSUES.md).

The report's referenced Arrow counterexamples were replayed during validation but remain local and
ignored; rerunning the command below regenerates them. They are not included in the source package.

The configuration retains the original wider 16-case exploratory campaign, but the
checked-in report intentionally contains only the two high-impact regression probes.
This keeps the upstream validation claim narrow and reproducible.

## Reproduce

This is an opt-in external integration study, not part of Parity's normal test suite.
It deliberately exits `1` while the recorded divergences remain present.

```bash
git clone https://github.com/pyjanitor-devs/pyjanitor.git /tmp/pyjanitor-parity
git -C /tmp/pyjanitor-parity checkout c1b57b993dca4348e9acc41301fe8526dcae57df

python -m venv /tmp/pyjanitor-parity-venv
/tmp/pyjanitor-parity-venv/bin/python -m pip install --upgrade pip
/tmp/pyjanitor-parity-venv/bin/python -m pip install \
  'parity-check==0.8.1' \
  'hypothesis==6.165.5' \
  'numpy==2.5.2' \
  'pandas==3.0.5' \
  'polars==1.43.2' \
  'pyarrow==25.0.1' \
  '/tmp/pyjanitor-parity'

cd case_studies/pyjanitor_complete
/tmp/pyjanitor-parity-venv/bin/parity check \
  --config parity.toml \
  --case null-key-preservation \
  --case narrow-domain-preservation \
  --no-performance \
  --json report.json \
  --markdown report.md
```

## Evidence and scope

- [pandas implementation](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/janitor/functions/complete.py)
- [Polars implementation](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/janitor/polars/complete.py)
- [pandas tests](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/tests/functions/test_complete.py)
- [Polars tests](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/tests/polars/functions/test_complete_polars.py)

The harness calls those public APIs through the small functions in
[`pyjanitor_parity.py`](pyjanitor_parity.py). This evidence applies to the pinned
versions and inputs; it is not a claim about every pyjanitor or dependency release.
