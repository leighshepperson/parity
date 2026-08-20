# Validation record

This campaign was rerun on 20 August 2026 with the Parity 0.12 development candidate. Each target
requirements file declares PyArrow plus its selected Pydantic release; neither environment
contained `parity-check` or its controller dependency tree.

The stable control passed all 54 observations. The migration surface ran 57 observations and
produced four distinct `ms3` findings: three exception-behaviour differences and one returned-value
difference. Hypothesis reduced each generated witness, and all four saved artifacts replayed
independently with exactly the stored finding signature.

The data-safe report separated the three exception findings by Pydantic error code: `missing`,
`string_type`, and `int_from_float`. It exposed no raw validation message or witness value.

The run therefore verified the properties this pilot is intended to stress:

- conflicting target dependencies remain isolated from the controller;
- a stable cross-version contract passes;
- return/raise and exception-semantic differences are classified as `FAILED`, not infrastructure
  `ERROR`;
- unrelated incompatibilities do not collapse into one finding;
- retained evidence is reproducible against recorded runtime provenance.

The migration case deliberately sets `max_findings = 4`, so its finding-limit notice means the
configured evidence budget was exhausted. It establishes the four expected historical behaviours;
it does not claim that those are every possible Pydantic 1-to-2 difference.

Generated artifacts and virtual environments are intentionally ignored. Recreate them from the
committed requirements and follow the command in [README.md](README.md).
