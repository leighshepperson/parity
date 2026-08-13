# Case study: pyjanitor `complete()` across pandas and Polars

On 13 August 2026, Parity compared pyjanitor's independently maintained pandas and Polars
implementations of `complete()` at upstream commit
[`c1b57b9`](https://github.com/pyjanitor-devs/pyjanitor/tree/c1b57b993dca4348e9acc41301fe8526dcae57df).
The study used only public APIs and synthetic data. Neither Parity nor pyjanitor was modified to
produce the result.

## Result

- 16 campaigns and 1,643 paired comparisons (3,286 implementation calls)
- 8 campaigns passed across 1,635 comparisons
- 8 focused fixtures found reproducible cross-backend divergences
- every failure replayed from its saved Arrow artifact
- the relevant upstream backend-specific tests still passed: 44 passed, 1 expected failure

The strongest findings were data-preservation gaps in the Polars implementation:

1. Existing rows with null completion keys retained their keys but lost their payload values.
2. Existing rows outside a supplied completion domain were omitted.

Parity also found differences in partial fill behaviour, empty input, a collision with an internal
`__index__` marker, column order, grouped sort order and unspecified unsorted row order. The first
two findings are probable correctness defects. The others may be defects or contracts that
pyjanitor should document explicitly.

## Campaign summary

| Campaign | Comparisons | Result | Contract exercised |
|---|---:|:---:|---|
| `grid-non-null` | 305 | pass | Cartesian completion and sorted joins |
| `fill-implicit` | 305 | pass | Fill only rows introduced by completion |
| `fill-explicit` | 305 | pass | Fill existing and introduced nulls |
| `grouped-inventory-content` | 305 | pass | Per-group completion, ignoring row order |
| `nested-observed-pairs` | 205 | pass | Cross a group with observed compound pairs |
| `mixed-logical-dtypes` | 206 | pass | Nullable scalar and temporal types |
| `duplicate-multiplicity` | 2 | pass | Preserve duplicate matched rows |
| `unsorted-row-content` | 2 | pass | Same row multiset when order is unspecified |
| `null-key-preservation` | 1 | fail | Existing null-key payloads are preserved |
| `narrow-domain-preservation` | 1 | fail | Existing rows outside a supplied domain survive |
| `partial-fill-dictionary` | 1 | fail | A partial fill mapping fills only named columns |
| `empty-input` | 1 | fail | Empty input has a shared result contract |
| `internal-name-collision` | 1 | fail | User column `__index__` does not collide internally |
| `original-column-order` | 1 | fail | Input column order is preserved |
| `grouped-sort-order` | 1 | fail | `sort=True` aligns grouped output order |
| `unsorted-row-order` | 1 | fail | Backend-native `sort=False` order happens to align |

The [machine-readable report](report.json) and [redacted Markdown report](report.md) record the
exact counts and mismatch classes. Compared row values are omitted from `report.md`.
Copy-ready reports for the two probable data-loss defects are in
[`UPSTREAM_ISSUES.md`](UPSTREAM_ISSUES.md).

## Reproduce

This is an opt-in external integration study, not part of Parity's normal test suite. It deliberately
exits `1` while the recorded divergences remain present.

```bash
git clone https://github.com/pyjanitor-devs/pyjanitor.git /tmp/pyjanitor-parity
git -C /tmp/pyjanitor-parity checkout c1b57b993dca4348e9acc41301fe8526dcae57df

python -m venv /tmp/pyjanitor-parity-venv
/tmp/pyjanitor-parity-venv/bin/python -m pip install --upgrade pip
/tmp/pyjanitor-parity-venv/bin/python -m pip install \
  'parity-check==0.1.0' \
  '/tmp/pyjanitor-parity' \
  'pandas==3.0.5' \
  'polars==1.43.2' \
  'pyarrow==25.0.1'

cd case_studies/pyjanitor_complete
/tmp/pyjanitor-parity-venv/bin/parity check \
  --config parity.toml \
  --no-performance \
  --json .parity-pyjanitor-final/full-report.json \
  --markdown .parity-pyjanitor-final/full-report.md
```

Run only the two high-impact regression probes for a quick verification:

```bash
/tmp/pyjanitor-parity-venv/bin/parity check \
  --config parity.toml \
  --case null-key-preservation \
  --case narrow-domain-preservation \
  --no-performance
```

## Evidence and scope

- [pandas implementation](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/janitor/functions/complete.py)
- [Polars implementation](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/janitor/polars/complete.py)
- [pandas tests](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/tests/functions/test_complete.py)
- [Polars tests](https://github.com/pyjanitor-devs/pyjanitor/blob/c1b57b993dca4348e9acc41301fe8526dcae57df/tests/polars/functions/test_complete_polars.py)

The harness calls those public APIs through the small functions in
[`pyjanitor_parity.py`](pyjanitor_parity.py). Fixtures are synthetic. This study demonstrates a
specific tested version pair; it is evidence of observed divergence, not proof about every input
or later release.
