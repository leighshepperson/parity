# Finding register

This register is backed by live Parity 0.9.2 execution against PyTimeTK 2.5.1 at commit
`c472ba5406791fbe7b37c902f18f7d5be64b46a5`. Stock and repaired candidates were run against the
same frozen configs in both dependency lanes.

| ID | Confirmed stock-candidate signal | Covering evidence | Repair evidence |
|---|---|---|---|
| PTK-001 | Polars sorts null timestamps before valid timestamps while pandas sorts them last, changing lag and rolling values. | Both baseline reports: `lags-null-semantics` and `rolling-null-semantics`, signature `ms1:7d353b6e…`. | `nulls_last=True` source repair; focused lag/rolling regressions; both final reports pass. |
| PTK-002 | Native Polars grouping computes values for a null group key that pandas' internal regrouping excludes. | Nullable fixtures in all four augmenters; value/shape evidence is included in signatures `ms1:7d353b6e…` and `ms1:ba791dbd…`. | Candidate nullable-group masks/fallbacks; focused grouped regressions; both final reports pass. |
| PTK-003 | Native EWM changes grouped row order and null-input carry behaviour. Release-era Polars also returns zero rather than null for the first unbiased standard deviation/variance. | `ewm-grouped-unsorted` (`ms1:41c13a73…`), `ewm-null-semantics` (`ms1:ba791dbd…`), and release `ewm-control` (`ms1:5b240284…`). | Version-aware EWM kwargs plus identity-preserving pandas fallback; four focused EWM regressions; both final reports pass. |
| PTK-004 | A null close changes the Polars MACD recurrence and later signal/histogram values. | Both baseline `macd-null-semantics` cases, signature `ms1:ba791dbd…`. | Identity-preserving null fallback and null-last ordering; three focused MACD regressions; both final reports pass. |
| PTK-005 | Ungrouped Polars padding retains input column order and identifies constants before padding, while pandas moves the date column first and identifies constants after reindexing. | Both baseline `pad-control` cases, signature `ms1:652888d5…`. | Date-first schema and post-padding constant detection; focused ungrouped padding regression; both final reports pass. |
| PTK-006 | A nullable grouping key is silently excluded by pandas' legacy regrouping while stock Polars raises a join-key type error. | Both baseline `pad-null-semantics` cases, signature `ms1:a7a14380…`. | Typed grouped join and explicit legacy null-group exclusion; focused nullable-group padding regression; both final reports pass. |

## Captured outcomes

- Stock release lane: five failed units, six exclusions, zero execution errors; eight failing cases.
- Stock current lane: five failed units, six exclusions, zero execution errors; seven failing cases.
- Repaired release and current lanes: five passed units, six exclusions, and all 15 cases passed.
- Direct dependency drift: all ten pandas/Polars release-to-current cases passed.
- Every report-referenced stock counterexample replayed in its recorded runtime before the repaired
  candidate was restored: eight release artifacts and seven current artifacts.
- Baseline and final reports share manifest SHA-256
  `8aa46c660d1d8a789ae150fdea9345265f756625bfcd82bc75d445dbb047b10a`. Within each lane they also
  share the same effective-config hash (`821aef18…` release, `77b1966a…` current).

The generated controls retain `rtol=1e-9` and use `atol=1e-10`. The absolute tolerance was
calibrated above an observed `1.8189894035458565e-12` reduction residual around zero; it does not
close any structural, null, ordering, exception or recurrence finding above. Exact hostile fixtures
set `search=false`, so their reviewed rows are checked with stability repeats without silently
expanding into an inferred domain.

Matching exceptions do not count as a repaired business result. Do not weaken strict row or column
order, remove nullable rows, ignore generated columns, or widen the numerical policy to close an
item.
