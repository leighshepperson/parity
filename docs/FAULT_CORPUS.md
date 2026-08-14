# Semantic fault corpus

The fault corpus is Parity's executable definition of useful detection. Every case contains a
reference, a deliberately wrong candidate, a small deterministic witness, an input domain and a
corrected candidate. It is synthetic and derived only from public engine behaviour.

Run it from the repository root:

```bash
parity check --config examples/pandas_polars/parity.toml --no-performance
```

This command is expected to exit `1`. Success means Parity found and classified all six injected
defects, not that the candidate implementations passed.

## Initial cases

### Null join keys

Pandas merge semantics can match missing keys, while a default Polars join does not. The candidate
therefore loses the lookup value for a missing customer ID. The corrected example explicitly
normalises the key rather than relying on version-specific join keyword names.

Expected signal: value/null mismatch and a missing-value diagnosis.

### Null grouping keys

The reference retains a missing-region group. The translated candidate applies `drop_nulls` before
aggregation and silently deletes that bucket.

Expected signal: shape/row mismatch and missing-value/group-order diagnosis.

### Local-time day boundary

The contract assigns an event to its New York calendar day. The candidate formats the UTC date
before conversion, moving early-UTC events into the wrong local day.

Expected signal: datetime value mismatch minimized to a boundary timestamp.

### Integer narrowing

The reference returns nullable 64-bit quantities. The candidate casts to signed 8-bit with
non-strict conversion; values outside `[-128, 127]` become null.

Expected signal: dtype and/or value mismatch at a small boundary value.

### Stable ordering

The reference sorts by priority while preserving arrival order for ties. The candidate adds record
ID as a tie-breaker and changes equal-priority order.

Expected signal: row mismatch when strict order is enabled.

### Integer precision

The reference preserves an integer just above `2**53`. The candidate round-trips it through a
binary float, losing one unit while retaining the same integer output dtype. The comparison uses
zero numeric tolerances so the regression also guards Parity's own lossless numeric boundary.

Expected signal: an exact numeric value mismatch.

## Passing counterparts

```bash
python -m examples.pandas_polars.make_fixtures
parity check --config examples/pandas_polars/parity.fixed.toml --no-performance
```

The fixed campaign anchors each case with a tiny generated Parquet fixture. It should exit `0`.

## Contribution standard

A new corpus fault must provide:

1. A one-purpose reference and bad candidate.
2. A public documentation link or an explanation based on public language/library semantics.
3. A synthetic deterministic witness with no proprietary field names or values.
4. A schema broad enough for shrinking but narrow enough to avoid meaningless invalid input.
5. A corrected candidate.
6. An integrity test that directly demonstrates the injected defect.
7. The expected mismatch kind and diagnosis, without depending on an exact terminal string.

High-value future classes include outer-join cardinality, categorical ordering, decimal precision,
daylight-saving gaps/folds, rolling-window boundaries, resampling labels, index materialisation,
three-valued boolean logic, integer division, duplicate column names, string Unicode normalization,
aggregation identity on empty groups and non-associative parallel reductions.

The corpus is not a performance leaderboard and must not be padded with trivial syntax errors. Its
purpose is to make semantic migration risk concrete and prevent detector regressions.
