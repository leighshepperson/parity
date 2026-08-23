from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest

from parity import Invocation
from parity.artifacts import ArtifactStore
from parity.config import ConfigError, load_config
from parity.custom_generation import (
    CustomGenerationError,
    load_custom_generator,
    normalize_generated_input,
)
from parity.engine import replay_artifact, run_suite
from parity.models import CallableSpec, CaseConfig, ExampleResult, GenerationConfig, Status


def _write_project(path: Path) -> None:
    path.write_text(
        "import pandas as pd\n"
        "from hypothesis import strategies as st\n"
        "from parity import Invocation\n"
        "\n"
        "def identity(frame):\n"
        "    return frame.copy()\n"
        "\n"
        "def corrupt_from_two(frame):\n"
        "    result = frame.copy()\n"
        "    if int(result['x'].iloc[0]) >= 2:\n"
        "        result['x'] = result['x'] + 1\n"
        "    return result\n"
        "\n"
        "def strategy_inputs():\n"
        "    return st.integers(min_value=0, max_value=20).map(\n"
        "        lambda value: Invocation(args=(pd.DataFrame({'x': [value]}),))\n"
        "    )\n"
        "\n"
        "def iterable_inputs():\n"
        "    for value in range(1000):\n"
        "        yield Invocation(args=(pd.DataFrame({'x': [value]}),))\n",
        encoding="utf-8",
    )


def _write_config(path: Path, *, module: str, generator: str, max_examples: int = 50) -> None:
    path.write_text(
        f"""
version = 2
artifact_dir = ".parity"

[[cases]]
name = "custom"

[cases.reference]
target = "{module}:identity"
adapter = "pandas"

[cases.candidate]
target = "{module}:corrupt_from_two"
adapter = "pandas"

[cases.generation]
generator = "{module}:{generator}"
max_examples = {max_examples}
seed = 113
adversarial_examples = false

[cases.performance]
enabled = false
""",
        encoding="utf-8",
    )


def test_custom_strategy_preserves_shrinking_artifacts_seed_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_project(tmp_path / "custom_strategy_project.py")
    config_path = tmp_path / "parity.toml"
    _write_config(
        config_path,
        module="custom_strategy_project",
        generator="strategy_inputs",
    )
    monkeypatch.chdir(tmp_path)

    result = run_suite(load_config(config_path))

    case = result.cases[0]
    assert result.status is Status.FAILED
    assert case.generated_examples > 0
    assert case.deterministic_examples == 0
    assert len(case.failures) == 1
    failure = case.failures[0]
    assert failure.source == "generated:custom:shrunk"
    assert failure.artifact is not None
    manifest = json.loads((failure.artifact / "manifest.json").read_text(encoding="utf-8"))
    replay = json.loads((failure.artifact / "replay.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 113
    assert replay["case"]["generation"]["generator"] is None
    assert replay["case"]["invocation"] == {}
    assert replay["invocation"]["args"] == [{"kind": "arrow", "file": "input-000.arrow"}]
    with pa.ipc.open_file(failure.artifact / "input-000.arrow") as reader:
        assert reader.read_all().column("x").to_pylist() == [2]
    replayed = replay_artifact(failure.artifact)
    assert replayed.status is Status.FAILED
    assert replayed.cases[0].failures[0].finding_signature == failure.finding_signature


def test_plain_iterable_is_bounded_and_reported_as_generated(tmp_path: Path) -> None:
    _write_project(tmp_path / "custom_iterable_project.py")
    config_path = tmp_path / "parity.toml"
    _write_config(
        config_path,
        module="custom_iterable_project",
        generator="iterable_inputs",
        max_examples=2,
    )

    result = run_suite(load_config(config_path))

    assert result.status is Status.PASSED
    case = result.cases[0]
    assert case.generated_examples == 2
    assert case.deterministic_examples == 0
    assert case.examples_run == 2


def test_custom_generator_supports_complete_keyword_invocations() -> None:
    generated = normalize_generated_input(
        Invocation(
            kwargs={
                "left": pa.table({"key": [1]}),
                "right": pa.table({"key": [1], "value": [2]}),
            }
        )
    )

    assert isinstance(generated, Invocation)
    assert list(generated.kwargs) == ["left", "right"]
    assert all(isinstance(value, pa.Table) for value in generated.kwargs.values())


def test_custom_artifact_replays_the_exact_keyword_contract(tmp_path: Path) -> None:
    case = CaseConfig(
        name="join",
        reference=CallableSpec(target="project:legacy"),
        candidate=CallableSpec(target="project:replacement"),
        generation=GenerationConfig(generator="generators:join_inputs"),
    )
    artifact = ArtifactStore(tmp_path / ".parity").write_failure(
        case,
        Invocation(
            kwargs={
                "left": pa.table({"key": [1]}),
                "right": pa.table({"key": [1], "value": [2]}),
            }
        ),
        ExampleResult(source="generated:custom:1", status=Status.FAILED),
    )

    replay = json.loads((artifact / "replay.json").read_text(encoding="utf-8"))
    replay_case = replay["case"]
    assert replay_case["generation"]["generator"] is None
    assert replay_case["invocation"] == {}
    assert replay["invocation"]["kwargs"] == {
        "left": {"kind": "arrow", "file": "input-000.arrow"},
        "right": {"kind": "arrow", "file": "input-001.arrow"},
    }
    CaseConfig.model_validate(replay_case)


def test_custom_generator_rejects_non_invocation_values(tmp_path: Path) -> None:
    module = tmp_path / "invalid_generator.py"
    module.write_text("def values():\n    return [1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(CustomGenerationError, match=r"parity\.Invocation"):
        load_custom_generator(
            "invalid_generator:values",
            base_directory=tmp_path,
            max_examples=3,
        )


def test_generator_is_a_complete_exclusive_input_contract(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        """
version = 2

[[cases]]
name = "ambiguous"
[[cases.invocation.args]]
kind = "frame"
fixture = "fixture.csv"

[cases.reference]
target = "project:old"

[cases.candidate]
target = "project:new"

[cases.generation]
generator = "project:inputs"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"exactly one of invocation or generation\.generator"):
        load_config(path)


def test_custom_hypothesis_strategy_loader_keeps_strategy_semantics(tmp_path: Path) -> None:
    module = tmp_path / "strategy_generator.py"
    module.write_text(
        "import pyarrow as pa\n"
        "from hypothesis import strategies as st\n"
        "from parity import Invocation\n"
        "def values():\n"
        "    return st.integers(0, 5).map(\n"
        "        lambda x: Invocation(args=(pa.table({'x': [x]}),))\n"
        "    )\n",
        encoding="utf-8",
    )

    loaded = load_custom_generator(
        "strategy_generator:values",
        base_directory=tmp_path,
        max_examples=3,
    )

    assert loaded.uses_hypothesis
    assert loaded.strategy is not None
    assert loaded.examples == ()
