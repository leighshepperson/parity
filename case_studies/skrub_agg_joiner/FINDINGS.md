# Findings and classification

This study compares skrub's pandas and Polars implementations at commit
`55dc7f45e140ccb76e768e3e4b4193f4eac3d5aa`. All inputs are synthetic.

## Controls

- Ordinary direct numeric aggregation should pass.
- Ordinary numeric aggregation through public `AggJoiner.fit_transform` should pass.
- A group with one unique string mode should pass.
- Database nulls supplied through the Arrow-preserving pandas input contract should pass.

These controls show that a failure is not caused merely by the wrapper, group row order,
compatible numeric dtypes, or the presence of a canonical Arrow null.

## Durable library findings

### Null grouping keys

The direct pandas implementation uses `DataFrame.groupby(key)` with pandas' default
`dropna=True`, so it drops a null-key group. The Polars implementation returns the null-key
group. This is a stable semantic difference at the pinned skrub commit. It is intentionally
tested at the low-level aggregation boundary: the public self-join used by `AggJoiner` masks the
difference because neither backend matches the null key during its subsequent join.

### Tied mode

For a group whose modes are tied between `x` and `y`, pandas returns both modes while the Polars
implementation selects one with `.mode().first()`. The public `AggJoiner` result therefore has a
sequence-valued pandas cell and a scalar Polars cell. Repeated identical Polars evaluations can
select different tied values, so this is also a determinism problem.

The wrapper returns the raw public output. Parity's sequence-valued-cell canonicalization compares
the pandas sequence against the Polars scalar without flattening the library behavior.

## Version-sensitive library finding

### IEEE NaN aggregation

With native dataframe constructors, pandas treats IEEE NaN as missing in `count`, `sum`, and
`mean`; Polars distinguishes NaN from null and includes it in the count while NaN propagates
through sum and mean. This reaches the public `AggJoiner` result. The campaign deliberately uses
`pandas_input = "native"`: Arrow-backed pandas dtypes implement a different contract.

The behavior must be rerun in both locked dependency lanes. A finding that persists in both is a
cross-backend contract difference; a finding that starts or disappears at one lane is dependency
drift and should be reported with that boundary.

## Matrix outcome

| Case | Class | Floor | Current |
| --- | --- | ---: | ---: |
| `aggregate-numeric-control` | control | pass | pass |
| `aggjoiner-numeric-control` | control | pass | pass |
| `aggregate-unique-mode-control` | control | pass | pass |
| `aggregate-arrow-null-control` | control | pass | pass |
| `aggregate-null-key-finding` | durable finding | fail | fail |
| `aggjoiner-tied-mode-finding` | durable finding | fail | fail |
| `aggjoiner-ieee-nan-finding` | version-sensitive finding | fail | fail |

The backend-native assertions reproduced all three findings in both locked lanes.
