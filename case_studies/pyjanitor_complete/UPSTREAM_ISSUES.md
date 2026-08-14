# Draft upstream issues

These two reports are standalone and ready to paste into pyjanitor's issue tracker.
On 14 August 2026, searches across open and closed pyjanitor issues and pull requests
for combinations of `complete`, `polars`, `null`, `payload`, `domain`, `drop`, and
`preserve rows` found no equivalent report.

## Polars `complete()` loses values on rows whose completion keys contain nulls

**Title:** `polars: complete() loses payload values when completion keys contain nulls`

### Body

The Polars implementation of `complete()` retains null completion-key combinations,
but it does not join them back to their original rows. Payload values on those rows
become null.

This reproduces on pyjanitor `0.32.23` at current `dev` commit
`c1b57b993dca4348e9acc41301fe8526dcae57df`, with Polars `1.43.2` and Python
`3.12.13`:

```python
import janitor.polars  # registers the dataframe methods
import polars as pl

frame = pl.DataFrame(
    {
        "store": ["A", None, "A"],
        "sku": [1, 1, None],
        "metric": [10.0, 20.0, 30.0],
    },
    schema={"store": pl.String, "sku": pl.Int64, "metric": pl.Float64},
)

print(frame.complete("store", "sku", sort=True).rows())
```

Actual:

```text
[(None, None, None), (None, 1, None), ('A', None, None), ('A', 1, 10.0)]
```

Expected: the generated `(None, None)` combination may have a null payload, while
the two combinations already present in the input retain their values:

```text
[(None, None, None), (None, 1, 20.0), ('A', None, 30.0), ('A', 1, 10.0)]
```

The pandas implementation in the same commit preserves both payload values. The
Polars implementation currently performs a left join without enabling null-key
equality; that appears to prevent the existing null-key rows from matching.

Environment:

- Python 3.12.13
- pyjanitor 0.32.23 / `c1b57b993dca4348e9acc41301fe8526dcae57df`
- Polars 1.43.2

## Polars `complete()` drops existing rows outside an explicit completion domain

**Title:** `polars: complete() drops existing rows outside an explicit domain`

### Body

When an explicit completion domain is narrower than values already present, the
Polars implementation of `complete()` drops original rows outside that domain.

This reproduces on pyjanitor `0.32.23` at current `dev` commit
`c1b57b993dca4348e9acc41301fe8526dcae57df`, with Polars `1.43.2` and Python
`3.12.13`:

```python
import janitor.polars  # registers the dataframe methods
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
    ).rows()
)
```

Actual:

```text
[(2020, 'A', 10.0), (2020, 'B', 20.0), (2021, 'A', None), (2021, 'B', None)]
```

Expected: the requested 2020–2021 combinations are added without deleting the
existing 2019 record:

```text
[(2019, 'A', 9.0), (2020, 'A', 10.0), (2020, 'B', 20.0),
 (2021, 'A', None), (2021, 'B', None)]
```

The pandas implementation in the same commit uses an outer merge and preserves the
2019 row. The Polars implementation currently left-joins the input onto the generated
domain, which explains why input rows outside that domain cannot survive.

Environment:

- Python 3.12.13
- pyjanitor 0.32.23 / `c1b57b993dca4348e9acc41301fe8526dcae57df`
- Polars 1.43.2
