# Compare existing calls across a Pydantic upgrade

This is a real dependency-behaviour comparison, using synthetic order inputs.
The same 16-line application module runs unchanged against Pydantic 1.10.22 and
2.13.4. There is no deliberately broken candidate, generated input schema,
migration workspace or Parity configuration file.

The control passes. Five supplied calls expose four documented changes:

| Call | Pydantic 1 | Pydantic 2 |
|---|---|---|
| Ordinary complete order | Accepted | Accepted |
| Numeric label | Coerced to string | Rejected |
| Fractional quantity | Truncated to integer | Rejected |
| Omitted optional note | Defaults to `None` | Required field error |
| Numeric string reference | Coerced by the first union member | String preserved |

These are intentional library changes; Parity does not decide whether the
application should adopt them. It shows exactly which supplied requests change.
Source: [Pydantic migration guide](https://docs.pydantic.dev/latest/migration/).

From the repository root, install Parity and create the two target environments
using the pinned requirements in `../pydantic_version/environments/`:

```bash
python -m venv case_studies/pydantic_version/environments/reference/.venv
case_studies/pydantic_version/environments/reference/.venv/bin/python -m pip install \
  -r case_studies/pydantic_version/environments/reference/requirements.txt
python -m venv case_studies/pydantic_version/environments/candidate/.venv
case_studies/pydantic_version/environments/candidate/.venv/bin/python -m pip install \
  -r case_studies/pydantic_version/environments/candidate/requirements.txt

parity compare order_contract:validate_order order_contract:validate_order \
  --calls case_studies/pydantic_calls/calls.jsonl \
  --reference-workdir case_studies/pydantic_calls \
  --candidate-workdir case_studies/pydantic_calls \
  --reference-python case_studies/pydantic_version/environments/reference/.venv/bin/python \
  --candidate-python case_studies/pydantic_version/environments/candidate/.venv/bin/python \
  --record-distribution pydantic
```

The comparison exits `1` with four findings. Use `control.jsonl` to see a passing
comparison. The target environments contain Pydantic and PyArrow, not Parity.

The executable acceptance check also verifies that all four findings replay:

```bash
python case_studies/pydantic_calls/verify.py \
  --reference-python case_studies/pydantic_version/environments/reference/.venv/bin/python \
  --candidate-python case_studies/pydantic_version/environments/candidate/.venv/bin/python
```

No shrink claim is made for supplied calls. Use the maintained
[generated campaign](../pydantic_version/README.md) to search and shrink a wider
input domain. Neither study establishes that an entire application is compatible.
