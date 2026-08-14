# utilsforecast pandas/Polars evaluation control

This study checks the pandas and Polars implementations used by
[`utilsforecast.evaluate`](https://github.com/Nixtla/utilsforecast/blob/ce2c7ddc7b71228ece21edf72ef9567d7467c0ab/utilsforecast/evaluation.py)
at the `0.2.16` release commit
[`ce2c7ddc7b71228ece21edf72ef9567d7467c0ab`](https://github.com/Nixtla/utilsforecast/commit/ce2c7ddc7b71228ece21edf72ef9567d7467c0ab).
It evaluates one forecast with the public `mae` and `rmse` losses over a six-row synthetic
fixture. No external or user data is used.

The result has one row per `(unique_id, metric)`. That composite pair is the declared output key,
so backend-specific group ordering is irrelevant while missing, duplicate, or numerically different
results still fail. The valid finite fixture is a clean control and is expected to pass.

## Reproduce in an isolated environment

From the Parity repository root, using CPython 3.11 or later:

```sh
python3.12 -m venv /tmp/parity-utilsforecast-0216
/tmp/parity-utilsforecast-0216/bin/pip install \
  -r case_studies/utilsforecast_evaluate/environments/requirements.txt
/tmp/parity-utilsforecast-0216/bin/pip install -e .
(
  cd case_studies/utilsforecast_evaluate
  /tmp/parity-utilsforecast-0216/bin/parity check \
    --config parity.toml \
    --no-performance \
    --json reports/report.json \
    --markdown reports/report.md
)
```

Expected result: `evaluate-mae-rmse` passes and the command exits `0`. The committed report records
that result under CPython 3.12.13 with Parity 0.8.1 and the exact dependency versions above. This
refresh required no wrapper, fixture, policy, or target-project changes. The report contains no
compared row values; all inputs were derived from the synthetic fixture. Replay artifacts remain
local in `.parity-utilsforecast/` and are ignored.
