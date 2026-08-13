# pandas `merge_asof` / Polars `join_asof` valid-domain study

This synthetic study exercises frame-local generation constraints. Both input frames in
`asof-direction` are generated in ascending `time` order, which is a precondition of pandas
`merge_asof` and Polars `join_asof`. The reference uses the backward strategy and the deliberately
different candidate uses the forward strategy, so Parity can search the valid input domain for a
small observable difference instead of mostly generating rejected, unsorted calls.

The second case is a passing control. Its schema declares `start <= end` for every non-null row,
then compares equivalent pandas and Polars span calculations. Together the cases demonstrate
`sorted_by` and `row_comparison` without hiding either condition in wrapper code.

From the repository root:

```sh
python -m pip install -e ".[dev]"
(
  cd case_studies/pandas_polars_asof
  parity check --config parity.toml --no-performance
)
```

Exit code `1` is expected: `asof-direction` is a compatibility probe and
`valid-interval-control` should pass. Generated evidence is ignored under `.parity-asof/` because
it contains raw input values. This is not an upstream bug report; the two functions intentionally
request different documented strategies. See the official
[pandas `merge_asof` reference](https://pandas.pydata.org/docs/reference/api/pandas.merge_asof.html)
and [Polars `join_asof` reference](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join_asof.html).
