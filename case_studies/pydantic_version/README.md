# Pydantic 1-to-2 dependency-isolation campaign

This campaign runs the same contract module in two target environments that do not contain
Parity. The reference has Pydantic 1 and the candidate has Pydantic 2; only the PyArrow transport
is shared. It proves that the controller can verify an environment whose dependency graph cannot
install `parity-check`, which itself uses modern Pydantic.

`stable-model-validation` is the required control. `pydantic-v1-to-v2-semantics` lets Hypothesis
discover and shrink four independent historical changes: integer-to-string coercion,
fractional-to-integer coercion, the implicit default for an `Optional` field, and model-to-dict
equality. The second case intentionally fails; distinct semantic signatures must keep its value
and exception findings separate, and every retained finding must replay.

The pinned requirements are evidence inputs for this historical campaign, not Parity's supported
dependency versions. Only the orchestrating environment installs `parity-check`.

## Reproduce

From the repository root, create both target environments with your preferred environment tool,
using the two committed requirements files. Then run:

```sh
parity check --config case_studies/pydantic_version/parity.toml --no-performance
```

Expected result: the control passes, the migration-surface case reports four independently signed
behavioural findings, and the suite exits `1` for `FAILED`. A missing worker dependency, invalid
protocol response, or target import failure exits `2` for `ERROR` instead.
