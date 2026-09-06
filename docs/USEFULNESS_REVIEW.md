# Usefulness review — 6 September 2026

## Decision

Keep the comparison engine. Replace the initial workflow with direct comparison
of existing calls. A wholesale rewrite would discard working isolation, comparison
and replay code without solving the main adoption problem.

Parity is useful for a bounded job: check whether a replacement preserves observed
behaviour when the two implementations or dependency graphs cannot comfortably run
inside the same test process, and retain reproducible evidence of differences.
It is not an automatic migration verifier for an arbitrary repository.

## What was checked

The review started at commit `6b21533731e7810828bd5f0b21db073c3b08b5da`, version 0.20.1.
It examined the public API, CLI, configuration, execution, comparison, evidence,
tests and case studies, then installed and executed the project.

| Check | Observed result |
|---|---|
| Existing test suite on Python 3.12 | 878 passed; one environment-floor test intentionally skipped |
| Fresh `init` and `check` | Runnable starter; 13 examples passed |
| Existing Pydantic 1.10.22 → 2.13.4 campaign | Control passed 54 examples; migration case found four distinct differences across 57 examples |
| Replay of the four existing findings | Four verified, zero stale or erroneous |
| New direct order-validation comparison | One passing control; five supplied calls found four documented dependency changes; all four replayed |

The Pydantic targets were installed separately with their pinned requirements.
Neither target contains the Parity controller. These findings are real library
behaviour changes, not fabricated candidate defects. They are intentional upstream
changes and do not by themselves show that upgrading is wrong.

## Where the old product fell short

The default starter demonstrated the engine using generated toy functions. A user
with actual requests still had to describe argument domains in TOML or write a
custom Hypothesis generator. The live API accepted one seed invocation and a
strategy, not a straightforward batch of existing calls. Representing correlated
calls as independent argument values could change the input domain being checked.

The introductory material also presented many advanced capabilities before a user
had obtained a result from their own code. More features did not remove this initial
translation work. A tiny same-process comparison often needs only an assertion;
Hypothesis already supplies generation and shrinking.

## What changed

`parity compare old:run new:run --calls calls.jsonl` accepts complete calls directly.
It preserves their relationships, checks the entire input file before executing code,
and reuses the existing workers, policies and evidence protocol. Python and pytest
entry points offer the same workflow.

The direct path checks known requests with exact numeric comparison. It does not
invent inputs, silently enable benchmarks, or claim that retained calls were
minimized. Generated campaigns remain useful for domain exploration. The README
now starts with a runnable standard-library comparison and distinguishes the jobs
where ordinary tests are simpler.

## Remaining limits

Representative inputs and a faithful wrapper still require application knowledge.
An incomplete corpus can miss an important regression. A matching pair of exceptions
is not proof that the application works. External side effects need project-owned
isolation; an entire repository cannot be verified by supplying a path alone.

The repository's case studies establish technical feasibility. They do not establish
independent adoption, saved engineering time for a real team, willingness to pay or
product-market fit. No such demand claim follows from this review. The next useful
external evidence would be a developer using their own representative calls for an
actual upgrade and finding the workflow cheaper than their existing test harness.

## Sources

- [Pydantic migration guide](https://docs.pydantic.dev/latest/migration/)
- [Hypothesis](https://hypothesis.readthedocs.io/)
- [Existing generated campaign](../case_studies/pydantic_version/README.md)
- [Direct order-validation acceptance check](../case_studies/pydantic_calls/README.md)
- [Public project validation log](../case_studies/ADOPTION_LOG.md)
