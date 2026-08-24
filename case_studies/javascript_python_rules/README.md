# Recursive JavaScript to Python rules-engine migration

This synthetic case study exercises a recursive-JSON cross-language contract. A legacy JavaScript
rules engine and its Python rewrite receive a recursive expression AST, a variable context and a
keyword threshold. They return a nested JSON decision, score, labels and execution trace or raise
the same domain exception.

The correct Python port preserves the legacy language's rules:

- boolean operators short-circuit, so an unreachable expression is never evaluated;
- every matching rule contributes to the result; and
- a score equal to the configured threshold is allowed.

The deliberately naive port contains three realistic rewrite defects: it evaluates scores eagerly,
stops after the first matching rule and uses an exclusive threshold. A custom Hypothesis strategy
generates typed recursive expressions plus each fault family. Parity finds three distinct mismatch
signatures, shrinks their complete `Invocation(*args, **kwargs)` values, stores JSON-only artifacts
and replays every finding without importing the generator.

## Run the proof

Provision Python, Node.js and the already-runnable project targets, then run from this directory:

```bash
python verify.py --profile quick
```

The full profile raises each generated campaign to 250 examples:

```bash
python verify.py --profile full
```

Expected output ends with:

```text
PASS correct Python port agrees with JavaScript (... generated programs)
PASS naive Python port produced three distinct minimized findings
PASS every minimized invocation is recursive JSON throughout
PASS correct port passes all three retained regressions
PASS all three findings replay from an unrelated working directory
```

`reference_adapter.py` is the only bridge. Parity's command-adapter SDK gives it the complete JSON
call, and the bridge invokes the project-owned `legacy_rules.js` executable boundary. JavaScript
knows nothing about Parity. Compilation, package installation and runtime provisioning remain the
project or CI's responsibility.

The proof exercises recursive project-owned generation, positional and keyword JSON arguments,
nested JSON returns, Return/Raise semantics, cross-language execution, multi-finding discovery,
shrinking, artifact persistence and exact replay through the same engine as the other maintained
campaigns.
