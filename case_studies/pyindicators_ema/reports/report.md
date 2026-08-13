# Parity verification

**FAILED** — 1/2 cases passed in 4.438s.

| Case | Status | Examples | Findings | Runtime ratio | Memory ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| ema-finite-control | passed | 2 | 0 | — | — |
| ema-nullable-backend-divergence | failed | 1 | 1 | — | — |

## Findings

- **ema-nullable-backend-divergence**: 1 exception; 1 distinct mismatch signature
  - artifact: `.parity-pyindicators/ema-nullable-backend-divergence/20260813T230621.103334Z-093823bd3324`
  - Failure behaviour differs (high): The implementations return versus raise, or raise different exception types. Decide whether invalid-input behaviour is part of the public contract.

Compared row values are omitted from this summary. Reproduce failures from their artifacts.
