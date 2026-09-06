from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from parity.corpus import CorpusError, load_calls


def test_load_calls_preserves_correlated_arguments_and_line_numbers(tmp_path: Path) -> None:
    content = (
        b'{"args":[{"items":[1,2]},2],"kwargs":{"region":"GB"}}\n\n'
        b'{"args":[{"items":[9]},1],"kwargs":{"region":"US"}}\n{}\n'
    )
    path = tmp_path / "calls.jsonl"
    path.write_bytes(content)
    corpus = load_calls(path)
    assert corpus.sha256 == hashlib.sha256(content).hexdigest()
    assert corpus.invocation.strategy is None
    examples = corpus.invocation.deterministic
    assert [source for source, _ in examples] == ["calls:line:1", "calls:line:3", "calls:line:4"]
    assert examples[0][1].args == ({"items": [1, 2]}, 2)
    assert examples[1][1].args == ({"items": [9]}, 1)
    assert dict(examples[0][1].kwargs) == {"region": "GB"}
    assert examples[2][1].args == ()


@pytest.mark.parametrize(
    "invalid",
    [
        '{"args":["PRIVATE"],"unexpected":true}',
        '{"args":"PRIVATE"}',
        '{"kwargs":[]}',
        '{"args":[{"secret":"PRIVATE","secret":2}]}',
        '{"args":[],"args":["PRIVATE"]}',
        '{"kwargs":{"not-valid":"PRIVATE"}}',
        '{"args":[NaN]}',
        '{"args":[Infinity]}',
        '{"args":[1e999]}',
        '["PRIVATE"]',
        '"PRIVATE"',
        "null",
        '{"args":["PRIVATE"]',
        json.dumps({"args": ["PRIVATE" * 50_000]}),
    ],
)
def test_invalid_calls_are_rejected_without_values(tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text("{}\n" + invalid + "\n", encoding="utf-8")
    with pytest.raises(CorpusError, match="invalid call on line 2") as caught:
        load_calls(path)
    assert "PRIVATE" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("content", [b"", b" \n\t\n", b"\xff\n", b"x" * (1024 * 1024 + 1)])
def test_empty_non_utf8_and_oversize_inputs_fail(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_bytes(content)
    with pytest.raises(CorpusError):
        load_calls(path)


def test_missing_file_is_a_safe_error(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="FileNotFoundError") as caught:
        load_calls(tmp_path / "private-filename.jsonl")
    assert "private-filename" not in str(caught.value)


def test_corpus_limits_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text("{}\n{}\n", encoding="utf-8")
    monkeypatch.setattr("parity.corpus._MAX_CALLS", 1)
    with pytest.raises(CorpusError, match=r"exceeds .* calls"):
        load_calls(path)
    monkeypatch.setattr("parity.corpus._MAX_CALLS", 100)
    monkeypatch.setattr("parity.corpus._MAX_CORPUS_BYTES", 5)
    with pytest.raises(CorpusError, match="exceeds 64 MiB"):
        load_calls(path)
