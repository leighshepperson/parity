# Distilled contracts

A distilled contract turns confirmed Parity findings into a durable regression gate that executes
only the candidate. Its initial baseline stores each minimized invocation, the exact reference
Return/Raise observation, the comparison policy and the candidate launch configuration. It never
stores a reference endpoint and cannot invoke the retired implementation. A reviewed retirement
can promote stable candidate observations into the final baseline.

This is useful at the end of a migration: discover incompatibilities while both implementations
exist, fix the candidate against a compact contract, then remove the old package, runtime, source
or executable without losing those regression cases.

## Workflow

Collect a JSON report and private finding artifacts with the current Parity release:

```bash
parity check --no-performance --json .parity/report.json
```

While the candidate still differs, distill the report's signed findings:

```bash
parity contract distill .parity/report.json .parity/contracts/upgrade
```

Fix the candidate and verify directly when every difference should disappear:

```bash
parity contract verify .parity/contracts/upgrade \
  --json .parity/contract-status.json
```

Exit `0` means every stored expectation matched, `1` means the candidate produced a semantic
difference, and `2` means the contract or candidate could not be verified reliably. The JSON output
uses the ordinary data-safe suite-report schema.

`distill` accepts suite and migration reports. Use `--artifact-root PATH` when the report's
artifact directory was restored at a different location.

When a reviewed difference is intentionally retained, promote the stable candidate into a second
contract instead of weakening verification:

```bash
parity contract retire \
  .parity/contracts/upgrade \
  .parity/contracts/retired \
  --budget compatibility.toml
parity contract verify .parity/contracts/retired
```

Retirement checks every current difference against its exact case-scoped approval, observes the
candidate twice in fresh processes, then stores the candidate observation as the new baseline. It
records approval rationales and the prior contract digest. See
[Compatibility budgets and reference retirement](COMPATIBILITY_BUDGETS.md).

## What is captured

For every distinct case/finding signature, Parity verifies the source artifact's hashes and copies:

- the complete minimized positional/keyword invocation, including zero or many Arrow leaves,
  JSON values and frame sequences;
- the reference's Arrow or JSON return value, or its normalized exception semantics;
- reference input-mutation behaviour and runtime provenance;
- the exact comparison policy and timeout;
- the sanitized, project-relative candidate target, command, workdir, interpreter and required
  environment-variable names.

Verification starts a fresh candidate process for every example. It does not run generation,
shrinking, performance benchmarks, the reference target or a reference runtime. Current candidate
runtime provenance is reported but deliberately is not required to equal the captured runtime: a
dependency or language upgrade is the thing being checked.

Only signed semantic finding entries are distilled, including reviewed findings in a budget-passing
report. Ordinary passing campaign examples are not promoted into a contract, and a report with no
signed findings is rejected. A distilled contract is therefore a
focused regression corpus for differences Parity actually discovered, not a proof or a complete
recording of the reference's behaviour. Keep reviewed fixtures and schemas for broader future
search where the reference remains available.

## Location and privacy

The destination must be a new directory inside the project root recorded by the source artifacts.
This preserves project-relative candidate paths without trusting the shell's current directory.
Moving the whole project preserves the contract; moving the contract independently does not.

Contracts contain real input and baseline output data. Parity creates the directory privately and
adds a self-contained `.gitignore`; do not publish or upload it without reviewing the data. Every
data file is size- and SHA-256-bound by `contract.json`, and verification rejects missing, changed,
redirected or escaping files before candidate code runs. As with replay artifacts, the hashes
detect corruption; they are not a third-party signature. Verify only contracts and project code you
trust.

Candidate environment values are never persisted. The verifier requires the same variable names
to be set by the caller and injects their current values. Redacted static arguments or command
arguments are rejected rather than guessed. Candidate interpreters, workdirs and path-like
executables must remain inside the recorded project.

Distillation requires artifacts produced by a release that captures `reference.json` and its bound
output. Older artifacts are intentionally unsupported; rerun `parity check` before retiring the
reference.

## Python API and schema

```python
from parity import distill_contract, retire_contract, verify_contract

created = distill_contract(
    ".parity/report.json",
    ".parity/contracts/upgrade",
)
retired = retire_contract(created.path, ".parity/contracts/retired")
result = verify_contract(retired.path)
assert result.passed
```

`distill_contract` returns a typed `DistillationResult`; `retire_contract` returns a
`RetirementResult`; `verify_contract` returns the ordinary `SuiteResult`.
`DistilledContractManifest` is public, and its frozen Draft 2020-12 version 3 schema is available
with:

```bash
parity schema distilled-contract
```
