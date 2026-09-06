# Compare existing calls

```bash
parity compare old_orders:quote new_orders:quote --calls calls.jsonl
```

Use this when representative requests already exist in fixtures, examples or a
sanitized test corpus. It executes the supplied calls in file order and retains
reproducible differences. You do not need `parity.toml`, a schema or a generator.

## Calls file

Each nonblank line is one JSON object:

```jsonl
{"args":[{"plan":"basic","seats":1}],"kwargs":{"region":"GB"}}
{"args":[{"plan":"pro","seats":25}],"kwargs":{"region":"US"}}
```

`args` is an array of positional arguments; `kwargs` is an object of keyword
arguments. Both are optional: `{}` is a zero-argument call. Calls may have different
shapes. A dictionary payload must be wrapped in `args`, as above. Parity preserves
whole calls rather than combining each argument's values independently.

All lines are validated before either target runs. Unknown top-level fields,
duplicate keys, non-finite numbers, invalid UTF-8 and an empty corpus are errors.
Limits are 100,000 calls, 64 MiB per file and 1 MiB per line, plus the existing
Invocation limits (256 arguments of each kind, 256 KiB per JSON value and 512 KiB
of JSON argument data per call). An invalid line is reported by number, without its values.

JSONL inputs are JSON values. To test dataframe inputs or generate and shrink a
wider domain, use the [configured workflow](USER_GUIDE.md) or `parity.verify`.

## Dependency versions and checkouts

Existing environments:

```bash
parity compare migration:run migration:run --calls calls.jsonl \
  --reference-python .venv-old/bin/python \
  --candidate-python .venv-new/bin/python \
  --record-distribution your-library
```

Each Python target environment needs PyArrow and its own application dependencies.
It does not need Parity. Python paths are relative to the directory where you run
the command, even when a target has a different working directory. Virtual-environment
symlinks are preserved. Repeat `--record-distribution` to bind relevant package versions.

Existing source checkouts:

```bash
parity compare app:calculate app:calculate --calls calls.jsonl \
  --reference-workdir ../before \
  --candidate-workdir ../after
```

Combine these options when each checkout has its own environment. The directories
must expose importable targets; for a `src/` layout, install the corresponding project
in each target environment. The command does not install dependencies or alter a checkout.
Use [managed environments](USER_GUIDE.md) when you want Parity to prepare them.

## Results and replay

```bash
parity compare old:run new:run --calls calls.jsonl \
  --json .parity/report.json --junit .parity/junit.xml
parity evidence verify .parity/report.json
```

Exit codes: `0` means all supplied calls compared equal; `1` means a confirmed
difference; `2` means invalid setup or unreliable execution. Crashes, timeouts and
nondeterminism are errors. Equal exceptions count as matching behaviour. Input
mutation is checked. Numbers compare exactly unless you explicitly supply `--rtol`
or `--atol`. Performance is not measured.

Each side receives fresh arguments, and each worker persists across the corpus.
Calls must be repeatable: the engine repeats observations to check stability.
To test a stateful system, make each call contain a complete event stream and reset
the system inside its wrapper. This is not production traffic shadowing.

By default Parity retains up to ten distinct mismatch signatures. Similar failures
may share a finding. An execution error or reaching `--max-findings` stops the run;
the report gives the number of calls executed. A pass covers only the supplied
corpus. This path neither invents inputs nor claims to minimize them.

The evidence fingerprint includes a SHA-256 of the complete file bytes. Each retained
finding stores its exact call and line number, so `parity replay <artifact-directory>`
works after the calls file is removed. Runtime/source provenance checks still apply.
Reports omit values; private finding directories contain actual inputs and outputs.

## Python and pytest

```python
from parity import compare

result = compare("old_orders:quote", "new_orders:quote", calls="calls.jsonl")
assert result.passed
```

For endpoint settings, pass `CallableSpec` objects from `parity.models`, including
`python`, `workdir`, `record_distributions` or a command adapter. `comparison` accepts
a `ComparisonPolicy`; `artifact_dir`, `max_findings` and `timeout_seconds` are also
available. Supplied endpoint objects are copied, not modified.

```python
def test_upgrade(parity):
    parity.compare("old_orders:quote", "new_orders:quote", calls="tests/orders.jsonl")
```

The pytest fixture includes mismatch and artifact details in assertion failures.

The [Pydantic order-validation study](../case_studies/pydantic_calls/README.md) runs
this API against two genuinely incompatible dependency environments and checks
both a passing control and replay of four real behaviour changes.
