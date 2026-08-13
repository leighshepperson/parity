# Parity verification

**FAILED** — 0/1 cases passed in 1.510s.

| Case | Status | Examples | Findings | Runtime ratio | Memory ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| categorical-groupby-observed-default | failed | 1 | 1 | — | — |

## Findings

- **categorical-groupby-observed-default**: 1 shape; 1 distinct mismatch signature
  - artifact: `.parity-pandas-versions/categorical-groupby-observed-default/20260813T231237.164875Z-a4306389d093`
  - Observable behaviour differs (low): The candidate does not satisfy the configured equivalence policy for this input. The preserved counterexample is the authoritative reproduction.

Compared row values are omitted from this summary. Reproduce failures from their artifacts.
