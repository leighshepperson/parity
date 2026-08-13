# Parity verification

**FAILED** — 8/16 cases passed in 96.985s.

| Case | Status | Examples | Failures | Runtime ratio | Memory ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| grid-non-null | passed | 305 | 0 | — | — |
| empty-input | failed | 1 | 1 | — | — |
| original-column-order | failed | 1 | 1 | — | — |
| grouped-sort-order | failed | 1 | 1 | — | — |
| unsorted-row-order | failed | 1 | 1 | — | — |
| fill-implicit | passed | 305 | 0 | — | — |
| fill-explicit | passed | 305 | 0 | — | — |
| grouped-inventory-content | passed | 305 | 0 | — | — |
| nested-observed-pairs | passed | 205 | 0 | — | — |
| mixed-logical-dtypes | passed | 206 | 0 | — | — |
| duplicate-multiplicity | passed | 2 | 0 | — | — |
| unsorted-row-content | passed | 2 | 0 | — | — |
| null-key-preservation | failed | 1 | 1 | — | — |
| narrow-domain-preservation | failed | 1 | 1 | — | — |
| partial-fill-dictionary | failed | 1 | 1 | — | — |
| internal-name-collision | failed | 1 | 1 | — | — |

## Findings

- **empty-input**: 1 exception; artifact: `.parity-pyjanitor-final/empty-input/20260813T064847.646200Z-246a8e8221d0`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
  - Failure behaviour differs (high): The implementations return versus raise, or raise different exception types. Decide whether invalid-input behaviour is part of the public contract.
- **original-column-order**: 1 column; artifact: `.parity-pyjanitor-final/original-column-order/20260813T064850.033899Z-a5077a07025e`
  - Row ordering differs (medium): A group-by, join, unique operation or query optimizer may not preserve implicit order. Add an explicit sort if order is part of the contract; otherwise configure an order-insensitive comparison with stable keys.
  - Output schema differs (medium): Check index materialization, selected columns, aliases, join suffixes and aggregation column names.
- **grouped-sort-order**: 8 value; artifact: `.parity-pyjanitor-final/grouped-sort-order/20260813T064854.851570Z-cfb392e77b14`
  - Observable behaviour differs (low): The candidate does not satisfy the configured equivalence policy for this input. The preserved counterexample is the authoritative reproduction.
- **unsorted-row-order**: 12 value; artifact: `.parity-pyjanitor-final/unsorted-row-order/20260813T064856.125754Z-5249eae5e6f2`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
- **null-key-preservation**: 4 row; artifact: `.parity-pyjanitor-final/null-key-preservation/20260813T065005.809155Z-dd70fa41a5af`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
  - Row ordering differs (medium): A group-by, join, unique operation or query optimizer may not preserve implicit order. Add an explicit sort if order is part of the contract; otherwise configure an order-insensitive comparison with stable keys.
- **narrow-domain-preservation**: 1 row, 1 shape; artifact: `.parity-pyjanitor-final/narrow-domain-preservation/20260813T065008.415257Z-8482b5e81a5c`
  - Row ordering differs (medium): A group-by, join, unique operation or query optimizer may not preserve implicit order. Add an explicit sort if order is part of the contract; otherwise configure an order-insensitive comparison with stable keys.
- **partial-fill-dictionary**: 1 exception; artifact: `.parity-pyjanitor-final/partial-fill-dictionary/20260813T065011.166604Z-307263e1f1d5`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
  - Failure behaviour differs (high): The implementations return versus raise, or raise different exception types. Decide whether invalid-input behaviour is part of the public contract.
- **internal-name-collision**: 1 exception; artifact: `.parity-pyjanitor-final/internal-name-collision/20260813T065014.438969Z-e74662bfb672`
  - Missing-value semantics differ (high): The engines may distinguish null, NaN and nullable dtypes differently. Check filters, joins, grouping keys and aggregations rather than simply widening tolerance.
  - Failure behaviour differs (high): The implementations return versus raise, or raise different exception types. Decide whether invalid-input behaviour is part of the public contract.

Compared row values are omitted from this summary. Reproduce failures from their artifacts.
