# Threat model

## Scope and assumptions

Parity compares two user-selected computations on a machine or CI runner controlled by the user.
Targets may be Python callables or arbitrary protocol-speaking commands. The primary deployment is
a trusted engineering repository running reviewed internal code. Reference/candidate defects are
expected; actively malicious targets require an external sandbox.

This model covers the local controller, target processes, configuration, fixtures, reports, counterexample artifacts,
the composite GitHub Action and package supply chain. Hosted coordinators are outside the current
trust boundary.

## Assets

- Source code and dependency environments.
- Fixture and generated dataframe values.
- Credentials inherited by the process or CI job.
- Integrity of pass/fail evidence, policies and replay artifacts.
- Availability of the developer machine or runner.
- Release credentials and published packages.

## Trust boundaries

1. **Repository to Parity process:** TOML, targets, schemas, fixtures and optional custom generator
   code are project-controlled input.
2. **Controller to targets:** Arrow/JSON requests and callable/command specifications cross a
   process boundary.
3. **Target adapter to application/native libraries:** arbitrary project and extension code executes.
4. **Core to artifact store:** potentially sensitive counterexamples are persisted.
5. **Runner to GitHub:** redacted summaries and optionally sensitive artifact directories are sent.
6. **Source repository to PyPI:** release workflow creates public packages.

## Threats and controls

| Threat | Existing controls | Residual risk / operator action |
|---|---|---|
| A misspelt policy silently weakens a check | Strict models reject unknown fields and invalid ranges. | A valid but inappropriate policy still passes; require code review. |
| Candidate tampers with controller state | Separate target process and canonical serialized input. | Target retains OS-user access; use a container for hostile code. |
| Target hangs or crashes | Per-invocation timeout and structured process outcomes. | Child processes/native hangs can outlive naive termination; enforce cgroup/job limits. |
| Candidate mutates its input | Before/after input fingerprint and mutation mismatch. | External state and files are not transactionally monitored. |
| Crafted config imports unexpected code | Restricted target syntax; user explicitly owns config. | Import itself executes module code. Review changes to config and wrappers. |
| Custom generator compromises the driver | Explicit import target and bounded consumed examples. | The factory and strategy execute in-process without a timeout; review them or sandbox the entire command. |
| Path traversal overwrites unrelated files | Case names are constrained, safe artifact names and config-relative path resolution. | User-selected artifact/config paths remain trusted operator input. |
| Counterexample leaks private values | Local default, self-ignoring private root, report redaction, configurable Action upload/retention. | Artifacts contain values. Apply source data classification and access controls. |
| Logs leak values or secrets | Parity reports omit frame values and does not enumerate environment variables. | User callables can print arbitrary content; use clean test credentials and protected logs. |
| Forged/stale evidence is accepted | Hash-bound artifacts written to new timestamped directories plus replay metadata. | Local users can alter files; signed attestations would be needed across trust domains. |
| Dependency/package compromise | Audit, dependency review, CodeQL, protected trusted publishing and build attestation. | Consumers must pin/verify and control their dependency mirror. |
| PR changes verifier and candidate together | CI review and public tests. | High-assurance users should run an independently pinned verifier from a protected workflow. |
| Resource exhaustion from generated cases | Bounded rows/examples, deadlines, invocation timeouts. | Schemas up to 10,000 rows and native allocations can still be expensive; enforce host quotas. |
| GitHub Action command injection | Inputs are passed through environment variables/Bash arrays; version format is validated. | Callable/config content intentionally controls imported project code. |

## Security invariants

- A semantic mismatch must not be converted into a pass by reporting failure.
- Configuration errors and target/infrastructure uncertainty must produce `ERROR`, never `PASSED`.
- Report projections must not include input-frame or mismatched scalar payloads.
- A replay artifact must be bound to the input hash and original case identity.
- Performance evidence must never run in place of semantic verification.
- No optional network service may become required for local verification.

Tests should cover these invariants whenever the relevant layer changes.

## Abuse cases deliberately not solved in-process

Parity does not defend the current OS user against a malicious function, prevent all timing or
resource side channels, detect secrets inside arbitrary strings, guarantee deletion from remote
artifact backups, or prove that the reference is legitimate. These need execution sandboxing,
organizational data controls and code ownership/review respectively.

## Possible hardening

Possible directions include content-addressed artifact manifests, signature and
provenance verification, an OCI-based hardened executor profile, deterministic environment capture,
policy files protected separately from candidate changes and pluggable organization redaction. Each
must preserve the offline local path.
