# Pytest integration

Installing Parity registers the `parity` plugin through pytest's entry-point discovery.

## Configured suite assertion

```python
def test_orders_are_equivalent(parity):
    result = parity.check("parity.toml", cases={"orders"})
    assert result.cases[0].generated_examples > 0
```

The fixture calls `pytest.fail` with a compact mismatch/artifact summary if the result is not
passing. On success it returns the full `SuiteResult` for additional assertions.

Configuration can come from command-line options:

```bash
pytest --parity-config migrations/parity.toml --parity-case orders
```

Repeated `--parity-case` values are passed as a selected set. A test-level marker takes precedence:

```python
import pytest


@pytest.mark.parity(config="migrations/parity.toml", cases=["orders", "customers"])
def test_critical_migrations(parity):
    parity.check()
```

The marker accepts keyword arguments only. Unknown keys are usage errors.
An explicit empty `cases=[]` marker or `cases=set()` fixture call is also an error; omit the
selection entirely to run every configured case.

## Live assertion

```python
from parity import ComparisonPolicy, FrameSchema


def test_live_pair(parity, schema: FrameSchema):
    parity.verify(
        reference_transform,
        candidate_transform,
        schema=schema,
        reference_adapter="pandas",
        candidate_adapter="polars",
        comparison=ComparisonPolicy(row_order="ignore"),
        artifact_dir=".parity/pytest-live",
    )
```

All keyword arguments are forwarded to the public `parity.verify` API.

## One externally selected case

The `parity_case` fixture returns the single name supplied with `--parity-case`:

```python
def test_selected_migration(parity, parity_case):
    parity.check(cases={parity_case})
```

It intentionally requires exactly one option. Parity does not read configuration during pytest
collection or silently generate tests; this keeps unrelated test discovery deterministic. Use
ordinary `@pytest.mark.parametrize` when every known case should be a separate pytest item.
