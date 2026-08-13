# Parity verification

**FAILED** — 4/7 cases passed in 18.495s.

| Case | Status | Examples | Failures | Runtime ratio | Memory ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| aggregate-numeric-control | passed | 2 | 0 | — | — |
| aggjoiner-numeric-control | passed | 2 | 0 | — | — |
| aggregate-unique-mode-control | passed | 2 | 0 | — | — |
| aggregate-arrow-null-control | passed | 2 | 0 | — | — |
| aggregate-null-key-finding | failed | 1 | 1 | — | — |
| aggjoiner-tied-mode-finding | failed | 1 | 1 | — | — |
| aggjoiner-ieee-nan-finding | failed | 1 | 1 | — | — |

## Findings

- **aggregate-null-key-finding**: 1 row, 1 shape; artifact: `.parity-skrub/aggregate-null-key-finding/20260813T171014.227078Z-f40637763a85`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
  - Row content or multiplicity differs (high): One output contains a row for which the other has no equivalent. Check filtering, grouping-key treatment, join cardinality and duplicate preservation.
- **aggjoiner-tied-mode-finding**: 1 dtype, 4 value; artifact: `.parity-skrub/aggjoiner-tied-mode-finding/20260813T171020.100451Z-d7c18e044ca7`
  - Type resolution differs (high): One implementation inferred or promoted a different dtype. Confirm overflow, nullable integer, string, categorical and decimal behaviour before accepting compatibility.
- **aggjoiner-ieee-nan-finding**: 12 value; artifact: `.parity-skrub/aggjoiner-ieee-nan-finding/20260813T171023.122059Z-b43a7f1e2c75`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.

Compared row values are omitted from this summary. Reproduce failures from their artifacts.
