# Public prior art and sources

Parity deliberately builds on public testing ideas while targeting the gap between them.

- Hypothesis property-based generation and shrinking:
  <https://hypothesis.readthedocs.io/>
- Hypothesis pandas strategies:
  <https://hypothesis.readthedocs.io/en/latest/reference/strategies.html#pandas>
- pandas testing utilities:
  <https://pandas.pydata.org/docs/reference/testing.html>
- Polars dataframe equality testing:
  <https://docs.pola.rs/api/python/stable/reference/api/polars.testing.assert_frame_equal.html>
- Public pandas-to-Polars semantic differences:
  <https://docs.pola.rs/user-guide/migration/pandas/>
- Apache Arrow columnar interchange:
  <https://arrow.apache.org/docs/format/Columnar.html>
- DataComPy dataframe reconciliation:
  <https://capitalone.github.io/datacompy/>
- Datafold data-diff documentation:
  <https://www.datafold.com/data-diff/>
- QuantCo's public description of dataframe migration validation pain:
  <https://tech.quantco.com/blog/dataframely>

Parity combines these ideas into an integrated campaign: execute reference and candidate code,
generate semantically adversarial inputs, apply an explicit cross-implementation equivalence
policy, minimize a counterexample, preserve it as a replayable artifact, and report correctness
and performance in CI.
