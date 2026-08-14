# PyTimeTK 2.5.1 candidate repair

Base: PyTimeTK `v2.5.1`, commit
`c472ba5406791fbe7b37c902f18f7d5be64b46a5`.

`candidate.patch` is the upstreamable semantic repair. It:

- makes Polars date ordering match pandas by placing null dates last;
- normalizes the Polars EWM `min_periods`/`min_samples` API across supported
  Polars versions and requires two observations for unbiased std/variance;
- uses the established pandas fallback for EWM and MACD inputs containing
  null values, where native Polars state propagation is not equivalent, while
  preserving grouped row identity and input order;
- aligns each Polars augmenter with PyTimeTK 2.5.1's existing pandas behavior
  for nullable group keys (masked lag rows, placeholder rolling rows, and
  excluded EWM/MACD groups);
- makes ungrouped Polars padding match pandas column order and constant-column
  detection after rows have been inserted; and
- handles nullable Polars padding groups without a null-key join schema error,
  matching the frozen 2.5.1 pandas behavior that excludes those groups.

The fallback attaches an internal row id before recursing once through the
pandas implementation, then restores native Polars output in original order.
This is necessary to retain row and group identity for unsorted inputs. The
patch intentionally follows the frozen public behavior of 2.5.1; changing
pandas `pad_by_time` to retain nullable groups would be a separate API behavior
change, not a backend migration repair.

`local-version.patch` is deliberately separate. It labels the locally repaired
candidate as `pytimetk==2.5.1+parity.1`, allowing Parity's runtime provenance to
distinguish it from the untouched reference without putting a pilot-specific
version into an upstream source patch.

Apply and test from a clean checkout:

```bash
git checkout c472ba5406791fbe7b37c902f18f7d5be64b46a5
git apply /path/to/patches/candidate.patch
git apply /path/to/patches/local-version.patch  # candidate lane only
pytest -q /path/to/upstream_tests/test_backend_parity_regressions.py
```

The regression file exercises `augment_lags`, `augment_rolling`, `augment_ewm`,
`augment_macd`, and `pad_by_time` through their public APIs, including native
grouped Polars inputs, hostile nulls, unsorted rows, and strict output layout.
The final source repair was checked with the pilot's Polars 1.21.0 release lane
and Polars 1.43.2 current lane; those committed lane definitions provide the
reproducible dependency-version evidence.
