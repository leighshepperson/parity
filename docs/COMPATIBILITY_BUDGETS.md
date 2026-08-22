# Compatibility budgets and reference retirement

A compatibility budget makes an intentional difference explicit without turning off discovery.
It approves exact `(case, ms3 finding signature)` pairs with a required review rationale. Approved
findings stay visible in terminal, Markdown, JSON and JUnit output; they simply stop blocking the
case. Any new difference class, rejected/review-state entry, execution error or enforced
performance regression still fails.

Comparison tolerances remain the right budget for numerical and datetime error. The
`[cases.performance]` thresholds remain the right budget for speed and memory. Finding approvals
cover reviewed structural or semantic differences that those quantitative policies cannot express.

## Capture and review

First collect a complete data-safe report and private finding artifacts:

```bash
parity check --json .parity/report.json
parity budget init .parity/report.json compatibility.toml
```

The generated budget is bound to the report SHA-256 and puts every distinct finding in `review`.
Approve findings one at a time after inspecting/replaying their private evidence:

```bash
parity budget approve compatibility.toml orders ms3:0123... \
  --reason "The replacement intentionally returns an empty table instead of raising."
```

The command refuses signatures that were not captured in that budget and requires a non-blank
rationale. To make the policy part of every configured run, declare the contained path beside the
other top-level `parity.toml` settings:

```toml
version = 1
compatibility_budget = "compatibility.toml"
```

The budget must remain inside the configuration directory. Parity includes its complete validated
content in the effective-configuration hash. A case's `generation.max_findings` must be greater
than its number of approved signatures, including after CLI overrides, so an allow-list can never
consume the entire discovery limit and hide the first new difference.

An approval that no longer occurs is reported as “no longer observed” and does not fail: fixing an
accepted difference is an improvement. Remove obsolete entries when convenient. Mismatch
signatures classify data-free behavioural shapes; they are not root-cause proofs, authorization
tokens or value bounds. Review the private artifact before approval.

## Retire the reference

Distill the signed findings while the original report and artifacts still exist:

```bash
parity contract distill \
  .parity/report.json \
  .parity/contracts/reference-baseline
```

When the candidate is final, promote it into a new contract:

```bash
parity contract retire \
  .parity/contracts/reference-baseline \
  .parity/contracts/retired \
  --budget compatibility.toml
```

`retire` executes only the candidate, twice in fresh processes for every stored example. It blocks
on a crash, timeout, nondeterminism, unavailable runtime provenance, a budget captured from another
report, or any difference whose exact case/signature pair is not approved. If the candidate now
matches every reference expectation, `--budget` is unnecessary.

On success, the new version 2 contract:

- stores the final candidate observations as its baseline;
- retains minimized inputs, comparison policy and candidate launch configuration;
- records the used approval signatures and rationales;
- binds the prior reference-baseline `contract.json` by SHA-256; and
- contains no reference endpoint and does not copy the prior reference outputs.

Verify it, then remove the old implementation, package, runtime and source contract when your own
retention policy permits:

```bash
parity contract verify .parity/contracts/retired
```

The retired contract still covers only the distinct examples that were distilled. It is a durable
regression basis for discovered behavioural classes, not evidence that every possible input or
public behaviour was exercised.

## Python API

```python
from parity import (
    approve_compatibility_finding,
    capture_compatibility_budget,
    retire_contract,
)

capture_compatibility_budget(".parity/report.json", "compatibility.toml")
approve_compatibility_finding(
    "compatibility.toml",
    "orders",
    "ms3:0123...",
    reason="Reviewed intentional contract change.",
)
retired = retire_contract(
    ".parity/contracts/reference-baseline",
    ".parity/contracts/retired",
    budget="compatibility.toml",
)
```

The frozen schemas are available as `parity schema compatibility-budget`,
`parity schema suite-report` (version 4) and `parity schema distilled-contract` (version 2).
