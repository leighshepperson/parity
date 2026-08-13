# pandas `merge` / Polars `join` input-bundle study

This small synthetic study exercises Parity's multi-input campaign directly. The reference calls
`pandas.DataFrame.merge`; the candidate calls `polars.DataFrame.join` with the corresponding left
join. Both implementations receive independently adapted `left` and `right` frames from one joint
Hypothesis strategy.

The case deliberately leaves join keys nullable. pandas matches null keys during a merge, while a
Polars join does not treat nulls as equal by default. Parity should find that documented difference,
jointly shrink both frames, and save both Arrow inputs in one replay artifact.

From the repository root:

```sh
python -m pip install -e ".[dev]"
(
  cd case_studies/pandas_polars_join
  parity check --config parity.toml --no-performance --max-examples 500
)
```

Exit code `1` is expected because this is a compatibility probe. The smallest useful witness has a
null join key on each side and different payload columns. The generated evidence is ignored under
`.parity-join/` because it contains raw input values; inspect or replay it locally.

This is not an upstream bug report. The two libraries document different defaults. A real migration
can align the candidate by opting into the Polars version's null-key matching option
(`join_nulls=True` in Polars 1.0, renamed to `nulls_equal=True` in current releases) and choosing an
explicit order policy.
