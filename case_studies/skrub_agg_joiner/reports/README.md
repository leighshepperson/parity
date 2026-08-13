# Matrix reports

Each locked lane contains:

- `report.json` — machine-readable, data-safe aggregate result;
- `report.md` — human-readable result;
- `direct-repro.txt` — backend-native confirmation.

Replay artifacts are not committed. A fresh run writes them under the case-study root
`.parity-skrub/` directory; their manifests contain the environment provenance needed to
distinguish lanes. Full endpoint provenance is stored in replay artifacts because the reference
and candidate can use distinct interpreters.

These reports were generated from Parity 0.2.0 with native pandas input semantics,
sequence-valued cell canonicalization, and explicit `skrub` / `scikit-learn` recording.
