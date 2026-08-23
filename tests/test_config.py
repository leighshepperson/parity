from __future__ import annotations

import sys
from pathlib import Path

import pytest

from parity.config import ConfigError, load_config
from parity.provenance import effective_config_sha256

VALID = """
version = 2
artifact_dir = "artifacts"

[[cases]]
name = "orders"

[cases.reference]
target = "transforms:legacy"
pandas_input = "native"
record_distributions = ["Scikit_Learn", "skrub"]

[cases.candidate]
target = "transforms:rewritten"
adapter = "polars"

[[cases.invocation.args]]
kind = "frame"
fixture = "fixtures/orders.csv"
"""


def test_load_config_resolves_paths_from_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "parity.toml"
    config_path.parent.mkdir()
    config_path.write_text(VALID, encoding="utf-8")

    config = load_config(config_path)

    assert config.artifact_dir == (config_path.parent / "artifacts").resolve()
    invocation = config.cases[0].invocation
    assert invocation is not None
    assert invocation.args[0].fixture == (config_path.parent / "fixtures/orders.csv").resolve()
    assert config.cases[0].reference.workdir == config_path.parent.resolve()
    assert config.cases[0].reference.pandas_input == "native"
    assert config.cases[0].reference.record_distributions == ["scikit-learn", "skrub"]
    assert config.cases[0].candidate.pandas_input == "arrow"


def test_load_config_accepts_custom_generation_and_parallel_limits(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        """
version = 2
jobs = 4
native_threads = 1

[[cases]]
name = "portfolio"

[cases.reference]
target = "project:legacy"

[cases.candidate]
target = "project:replacement"

[cases.generation]
generator = "generators:portfolio"
seed = 81
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.jobs == 4
    assert config.native_threads == 1
    assert config.cases[0].generation.generator == "generators:portfolio"
    assert config.cases[0]._base_directory == tmp_path


def test_load_config_reports_toml_error(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text("[[", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_load_config_reports_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(VALID + "\nunknown = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid Parity configuration"):
        load_config(path)


def test_load_config_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration not found"):
        load_config(tmp_path / "missing.toml")


def test_load_config_accepts_mixed_fixed_and_generated_frames(tmp_path: Path) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        """
version = 2

[[cases]]
name = "join"

[cases.reference]
target = "transforms:legacy"

[cases.candidate]
target = "transforms:rewritten"

[cases.invocation.kwargs.left]
kind = "frame"
fixture = "left.csv"

[cases.invocation.kwargs.right]
kind = "frame"

[cases.invocation.kwargs.right.schema]
min_rows = 0
max_rows = 2

[[cases.invocation.kwargs.right.schema.columns]]
name = "id"
dtype = "integer"
""",
        encoding="utf-8",
    )

    invocation = load_config(path).cases[0].invocation
    assert invocation is not None
    assert invocation.kwargs["left"].fixture == (tmp_path / "left.csv").resolve()
    assert invocation.kwargs["right"].fixture is None


def test_load_config_preserves_positional_argument_order_and_resolves_fixtures(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        """
version = 2

[[cases]]
name = "join"

[cases.reference]
target = "transforms:legacy"

[cases.candidate]
target = "transforms:rewritten"

[[cases.invocation.args]]
kind = "frame"
name = "zebra"
fixture = "fixtures/zebra.csv"

[[cases.invocation.args]]
kind = "frame"
name = "alpha"
fixture = "fixtures/alpha.csv"
""",
        encoding="utf-8",
    )

    invocation = load_config(path).cases[0].invocation

    assert invocation is not None
    assert [argument.name for argument in invocation.args] == ["zebra", "alpha"]
    assert invocation.args[0].fixture == (tmp_path / "fixtures/zebra.csv").resolve()
    assert invocation.args[1].fixture == (tmp_path / "fixtures/alpha.csv").resolve()


def test_load_config_preserves_distinct_virtualenv_python_symlink_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "parity.toml"
    for name in ("old", "new"):
        executable = tmp_path / name / "bin" / "python"
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
    config_path.write_text(
        """
version = 2

[[cases]]
name = "versions"

[cases.reference]
target = "transform:run"
python = "old/bin/python"

[cases.candidate]
target = "transform:run"
python = "new/bin/python"

[[cases.invocation.args]]
kind = "frame"
fixture = "fixture.json"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    case = config.cases[0]

    assert case.reference.python == tmp_path / "old" / "bin" / "python"
    assert case.candidate.python == tmp_path / "new" / "bin" / "python"
    assert case.reference.python != case.candidate.python
    assert case.reference.python.resolve() == case.candidate.python.resolve()

    distinct_hash = effective_config_sha256(config, base_directory=tmp_path)
    case.candidate.python = case.reference.python
    same_hash = effective_config_sha256(config, base_directory=tmp_path)
    assert distinct_hash != same_hash


def test_load_config_expands_strict_cases_file_and_bounded_defaults(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "cases.toml").write_text(
        """
version = 2

[[cases]]
name = "orders"

[cases.reference]
target = "transforms:legacy"
environment = { MODE = "case" }
record_distributions = ["case-only"]

[cases.candidate]
target = "transforms:rewritten"
adapter = "arrow"

[cases.comparison]
rtol = 0.0001

[cases.generation]
max_examples = 7

[[cases.invocation.args]]
kind = "frame"
fixture = "fixtures/orders.csv"
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "parity.toml"
    config_path.write_text(
        """
version = 2
artifact_dir = "artifacts"
cases_file = "shared/cases.toml"

[case_defaults]
timeout_seconds = 60

[case_defaults.reference]
adapter = "pandas"
pandas_input = "native"
python = "environments/reference/python"
environment = { SHARED = "default", MODE = "default" }
record_distributions = ["shared-one", "shared-two"]

[case_defaults.candidate]
adapter = "polars"
python = "environments/candidate/python"

[case_defaults.comparison]
row_order = "strict"
dtype = "strict"
rtol = 0.001

[case_defaults.generation]
max_examples = 20
search = false

[case_defaults.performance]
enabled = false
repeats = 3
confidence_level = 0.9
bootstrap_samples = 500
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    case = config.cases[0]

    assert case.invocation is not None
    assert case.invocation.args[0].fixture == (tmp_path / "fixtures/orders.csv").resolve()
    assert case.timeout_seconds == 60
    assert case.reference.adapter == "pandas"
    assert case.reference.pandas_input == "native"
    assert case.reference.python == tmp_path / "environments/reference/python"
    assert case.reference.environment == {"SHARED": "default", "MODE": "case"}
    assert case.reference.record_distributions == ["case-only"]
    assert case.candidate.adapter == "arrow"
    assert case.candidate.python == tmp_path / "environments/candidate/python"
    assert case.comparison.row_order == "strict"
    assert case.comparison.dtype == "strict"
    assert case.comparison.rtol == 0.0001
    assert case.generation.max_examples == 7
    assert not case.generation.search
    assert not case.performance.enabled
    assert case.performance.repeats == 3
    assert case.performance.confidence_level == 0.9
    assert case.performance.bootstrap_samples == 500
    assert "cases_file" not in config.model_dump()
    assert "case_defaults" not in config.model_dump()


def test_callable_defaults_inherit_and_override_required_distributions(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "parity.toml"
    config_path.write_text(
        """
version = 2

[case_defaults.reference]
required_distributions = { Scikit_Learn = ">=2,<4", polars = ">=1,<2" }

[case_defaults.candidate]
required_distributions = { pandas = ">=2,<4" }

[[cases]]
name = "requirements"

[cases.reference]
target = "transforms:legacy"
required_distributions = { scikit-learn = ">=3,<4" }

[cases.candidate]
target = "transforms:rewritten"

[[cases.invocation.args]]
kind = "frame"
fixture = "unused.json"
""",
        encoding="utf-8",
    )

    case = load_config(config_path).cases[0]

    assert case.reference.required_distributions == {
        "polars": "<2,>=1",
        "scikit-learn": "<4,>=3",
    }
    assert case.candidate.required_distributions == {"pandas": "<4,>=2"}


def test_case_defaults_cannot_hide_the_invocation_contract(tmp_path: Path) -> None:
    config_path = tmp_path / "parity.toml"
    config_path.write_text(
        """
version = 2

[case_defaults]
invocation = { args = [] }

[[cases]]
name = "engines"

[cases.reference]
target = "transforms:run"

[cases.candidate]
target = "transforms:run"

[[cases.invocation.args]]
kind = "frame"
fixture = "unused.arrow"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="invocation"):
        load_config(config_path)


def test_reused_and_inline_cases_have_identical_effective_model_and_hash(
    tmp_path: Path,
) -> None:
    case_text = """
[[cases]]
name = "orders"

[cases.reference]
target = "transforms:legacy"

[cases.candidate]
target = "transforms:rewritten"

[cases.comparison]
rtol = 0.0001

[[cases.invocation.args]]
kind = "frame"
fixture = "fixtures/orders.csv"
"""
    (tmp_path / "cases-a.toml").write_text("version = 2\n" + case_text, encoding="utf-8")
    (tmp_path / "cases-b.toml").write_text("version = 2\n" + case_text, encoding="utf-8")
    defaults = """
[case_defaults]
timeout_seconds = 45

[case_defaults.reference]
adapter = "pandas"
python = "env/reference/python"

[case_defaults.candidate]
adapter = "polars"
python = "env/candidate/python"

[case_defaults.comparison]
dtype = "strict"
rtol = 0.001

[case_defaults.performance]
enabled = false
"""
    first_path = tmp_path / "first.toml"
    second_path = tmp_path / "second.toml"
    inline_path = tmp_path / "inline.toml"
    first_path.write_text(
        'version = 2\nartifact_dir = "artifacts"\ncases_file = "cases-a.toml"\n' + defaults,
        encoding="utf-8",
    )
    second_path.write_text(
        'version = 2\nartifact_dir = "artifacts"\ncases_file = "cases-b.toml"\n' + defaults,
        encoding="utf-8",
    )
    inline_path.write_text(
        """
version = 2
artifact_dir = "artifacts"

[[cases]]
name = "orders"
timeout_seconds = 45

[cases.reference]
target = "transforms:legacy"
adapter = "pandas"
python = "env/reference/python"

[cases.candidate]
target = "transforms:rewritten"
adapter = "polars"
python = "env/candidate/python"

[cases.comparison]
dtype = "strict"
rtol = 0.0001

[cases.performance]
enabled = false

[[cases.invocation.args]]
kind = "frame"
fixture = "fixtures/orders.csv"
""",
        encoding="utf-8",
    )

    first = load_config(first_path)
    second = load_config(second_path)
    inline = load_config(inline_path)

    assert first == second == inline
    assert effective_config_sha256(first, base_directory=tmp_path) == effective_config_sha256(
        inline, base_directory=tmp_path
    )

    changed = first.model_copy(deep=True)
    changed.cases[0].comparison.rtol = 0.01
    assert effective_config_sha256(changed, base_directory=tmp_path) != effective_config_sha256(
        first, base_directory=tmp_path
    )


@pytest.mark.parametrize(
    "root",
    [
        "version = 2\n",
        'version = 2\ncases_file = "cases.toml"\n[[cases]]\nname = "inline"\n',
    ],
)
def test_load_config_requires_exactly_one_case_source(tmp_path: Path, root: str) -> None:
    (tmp_path / "cases.toml").write_text("version = 2\ncases = []\n", encoding="utf-8")
    path = tmp_path / "parity.toml"
    path.write_text(root, encoding="utf-8")

    with pytest.raises(ConfigError, match="exactly one of cases or cases_file"):
        load_config(path)


@pytest.mark.parametrize(
    ("defaults", "message"),
    [
        ('tags = ["hidden"]', "tags"),
        ('reference = { target = "hidden:target" }', "target"),
        ("comparison = { typo = true }", "typo"),
        ("generation = { max_examples = 0 }", "greater than or equal to 1"),
    ],
)
def test_load_config_rejects_forbidden_or_invalid_case_defaults(
    tmp_path: Path,
    defaults: str,
    message: str,
) -> None:
    path = tmp_path / "parity.toml"
    path.write_text(
        f"""
version = 2

[case_defaults]
{defaults}

[[cases]]
name = "orders"

[cases.reference]
target = "transforms:legacy"

[cases.candidate]
target = "transforms:rewritten"

[[cases.invocation.args]]
kind = "frame"
fixture = "orders.csv"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("cases_text", "message"),
    [
        ('version = 2\ncases_file = "nested.toml"\n', "cases_file"),
        ("version = 2\nunknown = true\ncases = []\n", "unknown"),
        ("cases = []\n", "version"),
        ("version = 2\ncases = []\n", "at least 1 item"),
    ],
)
def test_load_config_rejects_non_case_content_in_cases_file(
    tmp_path: Path,
    cases_text: str,
    message: str,
) -> None:
    (tmp_path / "cases.toml").write_text(cases_text, encoding="utf-8")
    path = tmp_path / "parity.toml"
    path.write_text('version = 2\ncases_file = "cases.toml"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_load_config_restricts_cases_file_to_root_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.toml"
    outside.write_text("version = 2\ncases = []\n", encoding="utf-8")
    root = tmp_path / "project"
    root.mkdir()

    for declared in (str(outside), "../outside.toml", "missing.toml"):
        path = root / "parity.toml"
        path.write_text(f'version = 2\ncases_file = "{declared}"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match=r"cases_file|cases file"):
            load_config(path)
