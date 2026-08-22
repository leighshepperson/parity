# Stateful C++ to Python order-book migration

This synthetic case study exercises Parity's hardest supported migration boundary: a compiled,
stateful C++ reference rewritten as Python. It combines a named two-table input bundle, a custom
Hypothesis generator, variable-length Arrow output, exact domain exceptions, multi-finding
discovery, shrinking, replay and report-only performance evidence.

The input contract contains an ordered event stream plus instrument metadata. The engine applies
price-time priority, partial fills, cancellations and per-instrument lot sizes. Cancelling an order
that has already filled or been cancelled raises `legacy.exchange.InvalidCancel` on both sides.

The deliberately naive port contains three realistic rewrite defects:

- it chooses the oldest crossing order instead of the best price then earliest sequence;
- it stops after the first partial fill; and
- it reports lots as units instead of applying the instrument's lot size.

Those defects create five independent observable findings: three return-value/shape differences
and two opposite Return/Raise state differences caused by the corrupted residual book. Parity
reduces them to witnesses containing 2, 3, 7, 7 and 10 events and replays every exact signature.

## Run the proof

From this directory, build the project-owned reference executable and run the bounded profile:

```bash
mkdir -p bin
g++ -std=c++17 -O2 -Wall -Wextra -pedantic reference.cpp -o bin/legacy_orderbook
python verify.py --profile quick
```

The extended campaign raises the generated control from 120 to 750 examples, gives each distinct
finding search a 500-example budget and retains paired performance evidence:

```bash
python verify.py --profile full
python soak.py --calls 2000
```

Expected output ends with:

```text
PASS correct port agrees with C++ (... generated streams)
PASS naive port produced five distinct minimized findings
PASS correct port passes all five retained regressions
PASS all five findings replay from an unrelated working directory
PASS persistent adapter soak (2000 paired calls: 1000 returned, 1000 raised)
```

Normal CI compiles the reference and runs the quick profile. The separate scheduled/manual
cross-language workflow runs the full profile and 2,000-call persistent-adapter soak. The binary is
never committed, and compilation remains a project/CI responsibility rather than a Parity feature.

`reference_adapter.py` is the only translation layer. It maps canonical Arrow tables to the
reference program's small text protocol, maps native fills back to Arrow, and converts the native
inactive-cancel response into a semantic `TargetRaised`. The C++ binary itself needs neither Python
nor Parity.

Performance is report-only. The adapter starts the legacy CLI once per observation while the
Python implementation runs in a persistent worker, so the result measures this migration boundary
rather than language-level matching speed.
