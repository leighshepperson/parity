# Clean-room provenance

Parity is an original implementation begun on 13 August 2026 in this new repository.

The product object is semantic verification: it executes independently supplied reference and
candidate computations, searches their input domain and reports observable differences. It does
not contain an analytical application runtime, UI widgets, dashboard composition, internal data,
private schemas, employer code, private benchmarks, or private documentation.

Implementation work must use only:

- public Python, NumPy, pandas, Polars, Arrow, Hypothesis and pytest APIs;
- examples written specifically for this repository using synthetic domains;
- published behavioural documentation recorded in the repository; and
- original architecture, source, tests and prose recorded in this repository's history.

Contributors must not paste private source, prompts, fixtures, metrics, architectural documents or
customer/employer data into issues, commits, tests, AI tools or examples. Product names, sample
fields and fault cases must be generic or synthetic.

This provenance record is an engineering control, not a legal opinion or substitute for any
outside-activity approval required by a contributor's employment terms.
