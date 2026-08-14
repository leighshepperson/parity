# External validation log

This log records small, bounded checks against public projects. Timings are setup observations from
one Linux host, not benchmarks. The inputs are synthetic and the studies do not claim compatibility
outside their pinned versions and configured domains.

## 14 August 2026: pyjanitor `complete()`

- Target: pyjanitor `dev` commit
  `c1b57b993dca4348e9acc41301fe8526dcae57df`, pandas `3.0.5`, Polars `1.43.2`,
  PyArrow `25.0.1`, and Parity `0.8.1`.
- Clean setup: 15.3 seconds to clone and detach the checkout, then 41.2 seconds to create the
  environment and install the pinned dependencies and source checkout. `pip check` passed.
- Friction: pip initially tried a read-only default cache while building the upstream source.
  Setting `PIP_CACHE_DIR` to a temporary writable directory resolved it. The PyPI package carrying
  the same `0.32.23` version label did not contain the pinned `dev` implementation, so the exact
  source commit was required.
- Outcome: two focused data-preservation findings reproduced through Parity and through standalone
  Polars calls, and both saved counterexamples replayed with the same mismatch signatures. Searches
  of open and closed issues and pull requests found no equivalent report. Two standalone issue
  drafts are ready; no upstream code pull request was opened before maintainer triage.

See the [study, evidence and issue drafts](pyjanitor_complete/README.md).

## 14 August 2026: utilsforecast `evaluate()`

- Target: utilsforecast `0.2.16` release commit
  `ce2c7ddc7b71228ece21edf72ef9567d7467c0ab`, pandas `2.3.3`, Polars `1.31.0`,
  Narwhals `2.15.0`, PyArrow `23.0.1`, and Parity `0.8.1`.
- Clean setup: about 38 seconds to create an environment and install the exact study dependencies
  plus the local Parity source, using a warm package cache. `pip check` passed.
- Friction: none beyond installing the pinned public-project dependencies. The existing wrapper,
  fixture, comparison policy and target source required no changes.
- Outcome: both workers passed `parity doctor`; two repeated observations agreed and the keyed
  pandas/Polars comparison produced no findings.

See the [passing-control study and report](utilsforecast_evaluate/README.md).
