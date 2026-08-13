# Upstream issue drafts

These are drafts only. Nothing has been filed upstream.

## Draft: AggJoiner returns different and nondeterministic results for tied modes

### Summary

`AggJoiner(operations="mode")` has different tied-mode behavior for pandas and Polars inputs.
Pandas returns every tied mode in a sequence-valued cell. Polars returns one scalar, and the
selected scalar can change across identical evaluations.

### Reproducer

```python
import pandas as pd
import polars as pl
from skrub import AggJoiner

data = {"key": ["A"] * 4, "label": ["x", "y", "x", "y"]}

for frame in (pd.DataFrame(data), pl.DataFrame(data)):
    result = AggJoiner(aux_table="X", operations="mode", key="key", cols="label").fit_transform(
        frame
    )
    print(result["label_mode"])
```

At skrub commit `55dc7f45e140ccb76e768e3e4b4193f4eac3d5aa`, pandas produces both `x` and
`y` for each joined row. Polars produces one of those strings. Repeating the Polars aggregation
can alternate between them because the implementation uses `pl.col(...).mode().first()` without
a deterministic ordering rule.

### Why it matters

The output cardinality/type differs between supported backends, and a fitted feature can change
without any input change. Either returning all tied modes or applying the same documented,
deterministic tie-break on both backends would give users a stable contract.

## Draft: AggJoiner numeric features disagree for IEEE NaN values

### Summary

`AggJoiner` produces different count, sum, and mean features for equivalent native pandas and
Polars inputs containing IEEE NaN values.

### Reproducer

```python
import pandas as pd
import polars as pl
from skrub import AggJoiner

data = {
    "key": ["A", "A", "B", "B"],
    "value": [1.0, float("nan"), float("nan"), 2.0],
}

for frame in (pd.DataFrame(data), pl.DataFrame(data)):
    result = AggJoiner(
        aux_table="X",
        operations=["count", "sum", "mean"],
        key="key",
        cols="value",
    ).fit_transform(frame)
    print(result)
```

In both the supported-floor and current locked lanes, pandas counts one non-missing value in each
group and produces finite sums and means. Polars counts two values and produces NaN sums and
means.

### Why it matters

The same public transformer yields materially different model features depending on dataframe
backend. If this is an intentional consequence of native backend semantics, documenting it next
to the list of supported operations would make the contract explicit. Otherwise, normalizing NaN
before aggregation could align the implementations.

## Not drafted

The null-group-key difference is recorded as evidence but not drafted as an upstream issue. It is
currently observed at skrub's internal aggregation boundary and is masked by the tested public
`AggJoiner` self-join output.
