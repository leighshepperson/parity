# Validation record

Executed on 2026-08-14 with Parity 0.9.2 and PyTimeTK commit
`c472ba5406791fbe7b37c902f18f7d5be64b46a5`.

## Static contract

- [x] `python make_fixtures.py` regenerates byte-identical committed Arrow fixtures.
- [x] The focused Parity contract tests pass.
- [x] `migration.toml` declares exactly five covered and six explicitly excluded units.
- [x] Release and current configs expose the same 15 cases and policies.
- [x] Reference and candidate interpreters are distinct in each lane.

## Baseline evidence

- [x] Stock PyTimeTK 2.5.1 returns migration exit `1`, not execution exit `2`, in the release lane.
- [x] Stock PyTimeTK 2.5.1 returns migration exit `1`, not execution exit `2`, in the current lane.
- [x] All 15 report-referenced baseline artifacts replay as semantic failures in their recorded
  stock runtimes (eight release, seven current).
- [x] Sanitized witnesses are promoted into fixtures and 13 focused upstream-style regressions.

## Repaired candidate

- [x] `patches/candidate.patch` and `patches/local-version.patch` apply cleanly to tag `v2.5.1`.
- [x] Candidate provenance reports `pytimetk==2.5.1+parity.1` in all 15 cases per lane.
- [x] All 13 focused repair regressions pass in both candidate environments.
- [x] Release `parity doctor` passes with stock 2.5.1 versus repaired 2.5.1+parity.1.
- [x] Current `parity doctor` passes with stock 2.5.1 versus repaired 2.5.1+parity.1.
- [x] Release migration result is five passed, six excluded and zero failed/error/uncovered.
- [x] Current migration result is five passed, six excluded and zero failed/error/uncovered.
- [x] All ten cases in `parity.version-drift.toml` pass with one exact fixture and no generated
  examples per case.

## Evidence hashes

- Manifest: `8aa46c660d1d8a789ae150fdea9345265f756625bfcd82bc75d445dbb047b10a`
- Release config: `821aef184819c570861ba3c6afbfbde18e7302083b97225430076834f99cd720`
- Current config: `77b1966a6bc09ef38e10046cef6c1b3f335fe749941ef28af51228657daea634`
- Version-drift config: `af09e2076537fc47903ed822637cd34326608f0f3ef42a4ebaa33758ad3b5292`

## Repository gates

- [x] Full Parity suite passes with warnings treated as errors: 476 tests, 74.78% branch coverage.
- [x] Ruff lint and format checks pass across 124 files.
- [x] Strict mypy passes for all 31 source files under Python 3.12 with a fresh cache; the CI
  Python 3.11 lane remains the authority for the declared floor.
- [x] `git diff --check` passes.
- [x] Fresh wheel and sdist build and pass `twine check`; the wheel smoke test passes.
- [x] Archive inspection excludes virtual environments, private artifacts, caches and all four
  disposable PyTimeTK source checkouts.
