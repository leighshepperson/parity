# Validation

The fixture generator, wrappers, direct assertions, and all seven campaigns were exercised in
both locked lanes.

The final campaign used explicit pandas input materialization, sequence-valued output
canonicalization, null/NaN comparison semantics, and `record_distributions` runtime provenance
from Parity 0.2.0. Data-safe floor and current reports were generated after the final validation.

| Case | Floor | Current |
| --- | ---: | ---: |
| `aggregate-numeric-control` | passed | passed |
| `aggjoiner-numeric-control` | passed | passed |
| `aggregate-unique-mode-control` | passed | passed |
| `aggregate-arrow-null-control` | passed | passed |
| `aggregate-null-key-finding` | failed as expected | failed as expected |
| `aggjoiner-tied-mode-finding` | failed as expected | failed as expected |
| `aggjoiner-ieee-nan-finding` | failed as expected | failed as expected |

Exact findings in both lanes:

- null key: reference has one aggregate row; candidate has two, including
  `[null, 1, 2.0, 2.0]`;
- tied mode: Parity reports list-versus-string dtype evidence plus a sequence-versus-scalar value
  mismatch; pandas supplies `[x, y]` while Polars supplies one scalar (`x` or `y`);
- IEEE NaN: every public output row has pandas `count=1` versus Polars `count=2`, with finite
  pandas sum/mean versus Polars NaN.

The target module SHA-256 matched
`ece034198b746e0d08e34c0e62ba2c892ac6be04d5710ce9c57d9ec967b51b7b` in both environments.
