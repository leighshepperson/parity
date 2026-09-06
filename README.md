# Parity

[![PyPI](https://img.shields.io/pypi/v/parity-check?label=PyPI)](https://pypi.org/project/parity-check/)
[![CI](https://github.com/leighshepperson/parity/actions/workflows/ci.yml/badge.svg)](https://github.com/leighshepperson/parity/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/parity-check)](https://pypi.org/project/parity-check/)
[![License](https://img.shields.io/github/license/leighshepperson/parity)](LICENSE)

**Run old and new code on the same calls. Keep reproducible evidence of what changed.**

Parity checks dependency upgrades, backend replacements and rewrites when the old
implementation still exists. Targets run in separate processes and can use different
Python environments, checkouts or languages. It compares returned values, exceptions
and input mutation, then saves confirmed differences for exact replay.

## Try a real behaviour difference

Install the controller with Python 3.11 or later:

```bash
python -m pip install parity-check
```

Save these requests as `calls.jsonl`:

```jsonl
{"args":["hello"]}
{"args":["hello world"]}
{"args":["a/b"]}
```

Compare two standard-library URL encoders:

```bash
parity compare urllib.parse:quote urllib.parse:quote_plus --calls calls.jsonl
```

The ordinary word matches. Spaces and slashes expose a behavioural difference.
Parity exits `1`, identifies the first differing call and retains an exact replay
artifact. This example needs no application code or configuration.

For your code, supply importable functions and representative requests:

```bash
parity compare old_orders:quote new_orders:quote --calls tests/orders.jsonl \
  --json .parity/report.json --junit .parity/junit.xml
parity evidence verify .parity/report.json
```

Each line is `{"args": [...], "kwargs": {...}}`; either field may be omitted.
Whole calls stay together, including correlated arguments. Exit codes are `0` for
matching behaviour, `1` for differences and `2` for invalid or unreliable execution.
Numbers compare exactly by default; set `--rtol` or `--atol` to accept a tolerance.

**A pass covers the supplied calls, not every possible input.** Direct comparisons
do not generate or shrink inputs. Empty and malformed corpora fail before targets run.
See the [direct comparison guide](docs/DIRECT_COMPARISON.md) for the full contract.

## When to use it

| Situation | Best starting point |
|---|---|
| Two simple functions in one environment | pytest, with Hypothesis if you need generated inputs |
| An upgrade needs conflicting dependencies | Parity with separate target environments |
| Existing requests must survive a rewrite | `parity compare --calls` |
| A dataframe migration needs order, dtype, null or numeric policies | A configured Parity campaign |
| Differences need repeatable CI evidence and exact replay | Parity reports and retained findings |
| No examples, reference or agreed behaviour exist | Define the contract first; Parity cannot infer it |

The value is the integrated isolation, comparison policy and evidence workflow.
It does not determine business intent or replace ordinary tests. The
[usefulness review](docs/USEFULNESS_REVIEW.md) records the evidence and limitations.

## Dependency versions and checkouts

Run an unchanged wrapper in two existing environments:

```bash
parity compare migration:run migration:run --calls calls.jsonl \
  --reference-python .venv-old/bin/python \
  --candidate-python .venv-new/bin/python \
  --record-distribution your-library
```

Each target environment needs its application dependencies and PyArrow, not the full
Parity installation. For different source checkouts, add `--reference-workdir ../before`
and `--candidate-workdir ../after`. The same module name can resolve to different source
on each side. Small wrappers can map different APIs into a shared input/output contract.

To have Parity prepare independently locked environments:

```bash
parity migration init \
  --reference-path ../before-upgrade \
  --candidate-path ../after-upgrade
```

Released targets use `--reference-package 'your-library==1.2.3'` and
`--candidate-package 'your-library==2.0.0'`. See the [user guide](docs/USER_GUIDE.md)
for setup, review and execution.

## Search beyond known examples

```bash
parity init
parity check
```

This creates a runnable example campaign. Replace its functions and input domain
with the behaviour you are migrating. Configured campaigns support Hypothesis search,
shrinking, dataframe schemas, relational inputs, multiple findings and optional
performance measurements after semantic success.

Parity's unit of work is an explicit `callable(*args, **kwargs)` contract. A complete
call can combine ordinary JSON, frames and project-generated structures such as
recursive programs and event streams. Install `parity-check[pandas]` or
`parity-check[polars]` when the controller needs those dataframe adapters.

Use [`parity.verify`](docs/PYTEST.md) for live functions and a Hypothesis strategy.
Cross-language targets use the [adapter SDK](docs/TARGET_ADAPTER_SDK.md).

For a configured campaign in GitHub Actions:

```yaml
- uses: leighshepperson/parity@v0
  with:
    config: parity.toml
    performance: "false"
```

## Python and pytest

```python
from parity import compare

result = compare("old_orders:quote", "new_orders:quote", calls="calls.jsonl")
assert result.passed
```

The pytest fixture adds a readable failure summary:

```python
def test_upgrade(parity):
    parity.compare("old_orders:quote", "new_orders:quote", calls="tests/orders.jsonl")
```

## Executable evidence

| Case | What it establishes |
|---|---|
| [Pydantic order requests](case_studies/pydantic_calls/README.md) | Five supplied calls expose four documented changes; control passes and all findings replay |
| [Generated Pydantic campaign](case_studies/pydantic_version/README.md) | Search and shrinking across conflicting dependency environments |
| [pandas 2.3 → 3.0](case_studies/pandas_version_groupby/README.md) | An unchanged group-by callable exposes a changed default |
| [PyTimeTK pandas → Polars](case_studies/pytimetk_migration/README.md) | Five public API comparisons with stock and repaired candidates |
| [JavaScript → Python](case_studies/javascript_python_rules/README.md) | Recursive programs, domain exceptions and minimized defects |
| [C++ → Python](case_studies/cpp_python_orderbook/README.md) | Stateful event streams and replay |

These are bounded engineering studies using synthetic inputs, not evidence that every
application is compatible or that independent teams have adopted Parity. More studies
are listed in the [external validation log](case_studies/ADOPTION_LOG.md).

## Evidence and boundaries

Terminal, JSON and JUnit reports omit compared values. Finding directories contain
actual inputs and outputs; keep them private. `parity replay <artifact-directory>`
reproduces a saved comparison and still exits `1` when the difference remains.
`parity evidence verify .parity/report.json` exits `0` when all findings reproduce.
Direct-comparison findings do not need the original calls file to replay.

Targets must be trusted and repeatable. Process isolation handles failures and
dependency conflicts; it is not a security sandbox. Wrappers own filesystem,
database and network cleanup. For stateful behaviour, pass a complete event stream
and reset state within the wrapper. Parity does not capture production traffic.

| Need | Documentation |
|---|---|
| Compare existing calls | [Direct comparison](docs/DIRECT_COMPARISON.md) |
| Build a generated campaign | [User guide](docs/USER_GUIDE.md) |
| Look up TOML fields | [Configuration reference](docs/CONFIG_REFERENCE.md) |
| Integrate with CI | [GitHub Action](docs/GITHUB_ACTION.md) |
| Review intentional differences | [Compatibility budgets](docs/COMPATIBILITY_BUDGETS.md) |
| Retire the old implementation | [Distilled contracts](docs/DISTILLED_CONTRACTS.md) |
| Understand execution and evidence | [Architecture](docs/ARCHITECTURE.md), [security](docs/SECURITY.md) |

Apache-2.0 licensed and pre-1.0. The current minor release is the supported line;
minor releases may change public contracts.
