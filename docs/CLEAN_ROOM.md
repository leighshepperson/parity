# Clean-room provenance

Parity is an original implementation begun on 13 August 2026 in a new repository.

Parity compares independently supplied reference and candidate computations, searches the configured
input domain and reports observable differences. It contains no private code, data, schemas,
benchmarks or documentation.

Implementation work must use only:

- public Python, NumPy, pandas, Polars, Arrow, Hypothesis and pytest APIs;
- examples written specifically for this repository using synthetic domains;
- published behavioural documentation linked from `docs/PRIOR_ART.md`; and
- original architecture, source, tests and prose recorded in this repository's history.

Contributors must not paste private source, prompts, fixtures, metrics, architectural documents or
third-party or employer data into issues, commits, tests, AI tools or examples. Example names,
fields and fault cases must be generic or synthetic.

This provenance record is an engineering control, not a legal opinion or substitute for any
outside-activity approval required by a contributor's employment terms.
