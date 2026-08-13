from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from parity.execution import _read_arrow, _write_arrow
from parity.worker import main, run_request


def test_worker_file_protocol(tmp_path: Path, monkeypatch) -> None:
    module = tmp_path / "worker_transform.py"
    module.write_text(
        "def transform(frame, amount):\n"
        "    result = frame.copy()\n"
        "    result['x'] += amount\n"
        "    return result\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    input_path = tmp_path / "input.arrow"
    output_path = tmp_path / "output.arrow"
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    _write_arrow(pa.table({"x": [1, 2]}), input_path)
    request_path.write_text(
        json.dumps(
            {
                "protocol_version": 2,
                "spec": {"target": "worker_transform:transform", "adapter": "pandas"},
                "input": str(input_path),
                "output_arrow": str(output_path),
                "output_json": str(tmp_path / "output.json"),
                "static_args": [3],
                "static_kwargs": {},
                "expected_runtime": None,
            }
        ),
        encoding="utf-8",
    )
    run_request(request_path, response_path)
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["outcome"] == "returned"
    assert response["protocol_version"] == 2
    assert response["runtime"]["python_implementation"]
    assert response["has_table"] is True
    assert "table" not in response
    assert _read_arrow(output_path).column("x").to_pylist() == [4, 5]


def test_worker_main_is_silent_on_bad_protocol(tmp_path: Path, capsys) -> None:
    request = tmp_path / "bad.json"
    request.write_text("{}", encoding="utf-8")
    assert main([str(request), str(tmp_path / "response.json")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert main([]) == 2
