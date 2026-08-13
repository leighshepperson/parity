# Draft upstream issues

These drafts isolate the two data-preservation findings from the broader study. They are ready to
paste into pyjanitor's public issue tracker after checking that no equivalent issue is already open.

## Polars `complete()` loses values on rows whose completion keys contain nulls

**Title:** `polars: complete() loses payload values when completion keys contain nulls`

### Description

The Polars implementation of `complete()` retains null completion-key combinations but does not
join them back to their original rows. Payload values on those rows become null.

This reproduces on pyjanitor `0.32.23` at commit
`c1b57b993dca4348e9acc41301fe8526dcae57df`, with Polars `1.43.2`:

```python
import janitor.polars  # noqa: F401
import polars as pl

frame = pl.DataFrame(
    {
        "store": ["A", None, "A"],
        "sku": [1, 1, None],
        "metric": [10.0, 20.0, 30.0],
    },
    schema={"store": pl.String, "sku": pl.Int64, "metric": pl.Float64},
)

print(frame.complete("store", "sku", sort=True))
```

Actual output includes these rows:

```text
("A", null, null)
(null, 1, null)
```

Expected output preserves the original payloads:

```text
("A", null, 30.0)
(null, 1, 20.0)
```

The corresponding pandas implementation preserves both values. This appears to come from the
Polars join not treating null keys as equal when the generated combinations are joined back to the
input. Setting the applicable join option to match null keys should preserve the original rows;
the exact fix should be tested against supported Polars versions.

### Environment

- Python 3.12.13
- pyjanitor 0.32.23 / `c1b57b9`
- Polars 1.43.2

## Polars `complete()` drops existing rows outside an explicit completion domain

**Title:** `polars: complete() drops existing rows outside an explicit domain`

### Description

When an explicit domain is narrower than the values already present, the Polars implementation of
`complete()` drops original rows outside that domain. The pandas implementation preserves original
rows while adding the requested combinations.

This reproduces on pyjanitor `0.32.23` at commit
`c1b57b993dca4348e9acc41301fe8526dcae57df`, with Polars `1.43.2`:

```python
import janitor.polars  # noqa: F401
import polars as pl

frame = pl.DataFrame(
    {
        "year": [2019, 2020, 2020],
        "product": ["A", "A", "B"],
        "value": [9.0, 10.0, 20.0],
    }
)

print(
    frame.complete(
        pl.Series("year", [2020, 2021]),
        "product",
        sort=True,
    )
)
```

Actual output has four rows for the 2020–2021 completion grid and omits the original
`(2019, "A", 9.0)` row.

Expected output has five rows: the four requested combinations plus the unchanged original 2019
row. The corresponding pandas call,
`frame.complete({"year": [2020, 2021]}, "product", sort=True)`, has that behaviour.

This appears to follow from joining the input onto the generated domain with a left join. An outer
join, followed by the function's documented sorting and column selection, should preserve existing
records outside the requested domain.

### Environment

- Python 3.12.13
- pyjanitor 0.32.23 / `c1b57b9`
- Polars 1.43.2
