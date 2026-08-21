# Fortran to Python in one container

This synthetic case study proves that Parity can verify a cross-language rewrite without becoming
a compiler, environment manager or container orchestrator. One Docker image contains:

- the Parity controller;
- a precompiled Fortran reference executable;
- correct and deliberately defective Python candidates; and
- one thin SDK command adapter that maps Arrow input to the Fortran program's text contract.

The numerical contract is Neumaier compensated summation over an ordered `float64` vector. The
correct Python port preserves the compensation. The defective port uses naive accumulation, a
plausible rewrite that passes ordinary examples but loses a small term during catastrophic
cancellation.

Run the complete proof from the repository root:

```bash
docker build --file case_studies/fortran_python/Dockerfile --tag parity-fortran-python .
docker run --rm --network none parity-fortran-python
```

The image uses a build stage to compile Fortran. The runtime image deliberately has no Fortran
compiler, Docker CLI, tox or uv. It runs a normal `parity doctor`, verifies the correct port over
generated inputs, finds and shrinks the defective port's cancellation mismatch, then replays the
same finding from an unrelated working directory.

`fortran_adapter.py` uses `parity.target_adapter` for the target protocol lifecycle, private-file
validation, Arrow/JSON transport and atomic responses. Its project-owned code is limited to
checking the compiled executable, validating the canonical `value` column, invoking Fortran and
parsing one canonical result. Start the same pattern in another project with:

```bash
parity adapter init adapters/reference.py
```

See the [command-adapter SDK guide](../../docs/TARGET_ADAPTER_SDK.md) for the generated API.

Expected final output is:

```text
PASS runtime contains no compiler, container CLI, tox or uv
PASS correct port agrees with Fortran
PASS naive port is rejected with a three-row counterexample
PASS replay reproduces the same finding from another working directory
```

The comparison is exact (`rtol = 0`, `atol = 0`) because both correct implementations perform the
same ordered IEEE binary64 operations. Performance is intentionally disabled: the Fortran adapter
starts a fresh native process for each observation while the Python candidate runs through
Parity's Python worker, so their timings do not represent equivalent boundaries.

This pattern keeps ownership clear. The project or CI builds the reference executable and image;
Parity starts the configured targets, generates inputs, compares behaviour, shrinks failures and
retains replayable evidence.
