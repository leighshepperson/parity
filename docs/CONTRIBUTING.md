# Contributing

Contributions are welcome when they make semantic evidence more accurate, reproducible or easier to
adopt.

Before opening a large implementation, start with a feature proposal containing a tiny synthetic
reference/candidate pair and the counterexample Parity should find. This keeps design discussion
grounded in observable behaviour.

## Clean-room requirement

Contribute only work you are entitled to contribute. Do not paste employer/customer code, private
schemas, production data, internal benchmarks, proprietary prompts or unpublished documentation
into commits, issues or AI tools. Use generic domains and artificial values. A contribution based on
public behaviour should cite the public source in `docs/PRIOR_ART.md` or its fault-corpus note.

## Pull requests

- Keep one semantic change per pull request.
- Add tests that fail without the change.
- Update public contracts and documentation together.
- Run all checks in the [development guide](DEVELOPMENT.md).
- Explain compatibility impact even when it is `none`.
- Do not weaken a default comparison merely to remove a failing test.

Small fixes may be submitted directly. Adapter, comparator, artifact-version and execution-security
changes should include a design explanation and threat-model update where relevant.

By submitting a contribution, you agree that it is licensed under Apache License 2.0 and that you
have authority to provide it under those terms.

## Review priorities

Correctness and failure safety come first, then reproducibility, privacy, performance and ergonomic
convenience. A false pass is more serious than a noisy error. Reviewers may ask for a conservative
error outcome where evidence is ambiguous.

Security issues belong in a private advisory, not a pull request or public issue.
