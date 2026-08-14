# Parity verification

**FAILED** — 0/2 cases passed in 4.714s.

| Case | Status | Examples | Findings | Runtime ratio | Memory ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| null-key-preservation | failed | 1 | 1 | — | — |
| narrow-domain-preservation | failed | 1 | 1 | — | — |

## Findings

- **null-key-preservation**: 4 row; 1 distinct mismatch signature
  - artifact: `.parity-pyjanitor-final/null-key-preservation/20260814T002610.786805Z-6ea29b4caa2c`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
  - Row content or multiplicity differs (high): One output contains a row for which the other has no equivalent. Check filtering, grouping-key treatment, join cardinality and duplicate preservation.
- **narrow-domain-preservation**: 1 row, 1 shape; 1 distinct mismatch signature
  - artifact: `.parity-pyjanitor-final/narrow-domain-preservation/20260814T002613.165182Z-b20645ab421e`
  - Row content or multiplicity differs (high): One output contains a row for which the other has no equivalent. Check filtering, grouping-key treatment, join cardinality and duplicate preservation.

Compared row values are omitted from this summary. Reproduce failures from their artifacts.
