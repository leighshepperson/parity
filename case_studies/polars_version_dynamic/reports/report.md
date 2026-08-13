# Parity verification

**FAILED** — 0/1 cases passed in 1.541s.

| Case | Status | Examples | Findings | Runtime ratio | Memory ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| group-by-dynamic-default-offset | failed | 1 | 1 | — | — |

## Findings

- **group-by-dynamic-default-offset**: 1 shape, 4 value; 1 distinct mismatch signature
  - artifact: `.parity-polars-versions/group-by-dynamic-default-offset/20260813T233434.008601Z-364de4eac4ba`
  - Datetime representation or timezone semantics differ (high): Check time units, timezone localization/conversion, daylight-saving boundaries and whether the transformation returns an instant or a wall-clock value.

Compared row values are omitted from this summary. Reproduce failures from their artifacts.
