# Python command-adapter SDK

Use the command-adapter SDK when a Python boundary needs to call a Fortran, C/C++, Rust, Java or
legacy executable. The project owns only the translation between canonical Arrow input and the
target's interface. The SDK owns target protocol v1: the persistent process, private paths, request
validation, Arrow/JSON transport, result classification and atomic responses.

The wrapped target does not need Python or Parity. The small Python adapter process does need the
base `parity-check` installation. Implement the raw [external target protocol](TARGET_PROTOCOL.md)
instead when the adapter itself must be non-Python or cannot install Parity.

## Scaffold an adapter

From the directory containing `parity.toml`:

```bash
parity adapter init adapters/reference.py \
  --program bin/legacy-target \
  --runtime legacy-runtime \
  --runtime-version 1.0
```

The command creates one deliberately incomplete module and refuses to overwrite an existing file
unless `--force` is supplied. The program path is relative to the generated adapter. Edit
`PROGRAM`, the runtime identity and `execute` if the supplied values are not already exact, then
configure it as an ordinary command endpoint:

```toml
[cases.reference]
command = ["parity", "adapter", "serve", "adapters/reference.py"]
```

The configured command deliberately omits the private session-root argument. Parity appends that
argument when it starts the endpoint. Relative adapter paths are resolved from the endpoint
`workdir`, which defaults to the directory containing `parity.toml`.

Preflight and run the case normally:

```bash
parity doctor --config parity.toml
parity check --config parity.toml
```

`parity adapter serve` is the protocol process used by the configured endpoint; it is not a
standalone behavioural test command.

## Implement the project boundary

A complete adapter has one `CommandAdapter` object exported as `adapter`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pyarrow as pa

from parity.target_adapter import (
    AdapterError,
    CommandAdapter,
    Return,
    RuntimeInfo,
    TargetRaised,
    require_executable,
)

PROGRAM = Path(__file__).resolve().parent / "bin" / "legacy-calculate"


def inspect_target() -> None:
    """Check target availability without invoking the behaviour under test."""

    require_executable(PROGRAM)


def execute(frame: pa.Table) -> Return:
    """Map canonical input to the legacy interface and its result back to the contract."""

    payload = "\n".join(str(value) for value in frame["value"].to_pylist()) + "\n"
    try:
        completed = subprocess.run(
            [str(PROGRAM)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError("target_invocation", "legacy target could not be invoked") from exc

    if completed.returncode == 2:
        raise TargetRaised(
            "input rejected",
            module="legacy.domain",
            exception_type="InvalidInput",
            details={"error_codes": ["invalid_input"]},
        )
    if completed.returncode != 0:
        raise AdapterError("target_failure", "legacy target did not return a usable result")

    try:
        value = float(completed.stdout.strip())
    except ValueError as exc:
        raise AdapterError("invalid_output", "legacy target returned an invalid number") from exc
    return Return(value, return_type="legacy.real64")


adapter = CommandAdapter(
    runtime=RuntimeInfo(name="legacy-runtime", version="1"),
    inspect=inspect_target,
    execute=execute,
)
```

`RuntimeInfo(name, version, distributions=())` is stable, path-free provenance for the target
contract. The SDK records its own Parity, Python and PyArrow identity. Optional distribution names
ask it to report further versions installed in the adapter process; requirements remain declared
and enforced in `parity.toml`. Do not put paths, hostnames, command lines or secrets in runtime
labels.

`inspect` is optional. When present, it checks executables, imports and configuration without
invoking the behavioural operation. `require_executable(path)` performs the normal regular-file and
executable check and returns the resolved path. Keep compilation, installation and container setup
outside the adapter.

The SDK applies the configured input binding to `execute`:

- a single input is the first Arrow table argument;
- a positional bundle supplies its Arrow tables in declared order, before static arguments; and
- a keyword bundle supplies each Arrow table by its logical input name.

Configured static arguments and keyword arguments follow the same rules as Python endpoints. This
leaves `execute` looking like an ordinary project function instead of a protocol handler.

## Classify outcomes deliberately

Return a canonical Arrow table or JSON-compatible value directly for an ordinary success. The
optional `return_type` on `CommandAdapter` supplies its default descriptive label. Wrap a result or
raise an explicit SDK outcome when the observation needs more information:

- `Return(value, return_type=None, mutated_inputs=())` publishes the value while overriding the
  default return type or declaring mutation for that call. List logical input names in
  `mutated_inputs` only when mutation is part of the observable contract.
- `TargetRaised(message, module="builtins", exception_type="Exception", details=None,
  mutated_inputs=())` represents a deliberate application/domain rejection. A difference between
  the two sides is semantic evidence and therefore `FAILED`.
- `AdapterError(code, safe_message)` represents missing infrastructure, failed invocation,
  invalid transport mapping or unusable output. It makes the case `ERROR`, because no trustworthy
  behavioural comparison occurred.

Messages, codes and structured details must be bounded and safe to retain. Do not copy private
input, stderr, credentials, host paths or an unbounded caught exception into them. Convert only a
genuine target rejection to `TargetRaised`; a crash, timeout or parse failure is an `AdapterError`.

## Ownership and lifecycle

One adapter process serves many generated examples, confirmations and performance repeats. Avoid
hidden mutable state, clean up per-call resources and make `execute` deterministic. The SDK
deserializes a fresh Arrow input for each request and handles private-path validation and atomic
publication, but it is not a hostile-code sandbox. Run untrusted targets in a container or hardened
runner.

The project or CI remains responsible for compiling the target, building the image, installing
dependencies and pinning the executable. Parity remains responsible for generating inputs,
executing both endpoints, comparing outcomes, shrinking differences and retaining replay evidence.
The [Fortran-to-Python case study](../case_studies/fortran_python/README.md) demonstrates this split
in one multi-stage Docker image. The
[C++-to-Python order-book study](../case_studies/cpp_python_orderbook/README.md) extends it to a
stateful, multi-table contract with domain exceptions, multi-finding replay and a persistent
adapter soak.
