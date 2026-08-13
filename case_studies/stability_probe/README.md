# Same-input stability probe

This controlled study demonstrates a failure that ordinary differential comparison can miss. The
reference and candidate have identical hidden call-counter behaviour, so their first outputs agree.
Neither output is stable for the same input, however: the second observation adds a different
`observation` value.

Parity repeats deterministic passing inputs according to `generation.stability_repeats` and
classifies that drift as an unsigned execution error. It does not accept a semantic pass or a
counterexample from a computation whose observation is not repeatable.

From the repository root:

```sh
python -m pip install -e ".[dev]"
(
  cd case_studies/stability_probe
  parity check --config parity.toml --no-performance
)
```

Exit code `2` is expected. This is a synthetic contract test, not a claim about an upstream
library. Set `stability_repeats = 1` only when repeated observation is intentionally disabled.
