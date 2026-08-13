# Pandas-to-Polars fault corpus

This is an executable teaching corpus, not a benchmark. It contains five deliberately wrong
migrations over synthetic data:

| Case | Preserved contract | Injected defect |
|---|---|---|
| `null-join` | Missing keys match | Default join leaves the missing key unmatched |
| `groupby-null` | Missing-region bucket is retained | Candidate drops it before aggregation |
| `timezone-day` | Day boundary is New York local time | Candidate extracts the UTC day |
| `dtype-width` | Nullable 64-bit quantity | Candidate narrows to 8 bits |
| `stable-order` | Equal priorities retain arrival order | Candidate adds a different tie-breaker |

From the repository root, run the expected-failure campaign:

```bash
parity check --config examples/pandas_polars/parity.toml --no-performance
```

Each failure should produce a minimized replay directory under `.parity/fault-corpus`.

To run corrected implementations, first generate the small Parquet seeds, then use the passing
configuration:

```bash
python -m examples.pandas_polars.make_fixtures
parity check --config examples/pandas_polars/parity.fixed.toml --no-performance
```

The code uses only public pandas and Polars behaviour and artificial customer/order fields. See
the repository clean-room and prior-art records for provenance.
