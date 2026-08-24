from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONFIG_REFERENCE = ROOT / "docs" / "CONFIG_REFERENCE.md"
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
TOML_BLOCK = re.compile(r"```toml\n(.*?)\n```", re.DOTALL)


def documentation_paths() -> list[Path]:
    paths = [ROOT / "README.md", ROOT / "CHANGELOG.md"]
    for directory in (ROOT / ".github", ROOT / "docs", ROOT / "examples", ROOT / "case_studies"):
        paths.extend(directory.rglob("*.md"))
    return sorted(
        path
        for path in paths
        if not any(part == ".venv" or part.startswith(".parity") for part in path.parts)
    )


def heading_slugs(text: str) -> set[str]:
    seen: dict[str, int] = {}
    slugs: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", text, re.MULTILINE):
        base = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        base = re.sub(r"-+", "-", base)
        duplicate = seen.get(base, 0)
        seen[base] = duplicate + 1
        slugs.add(base if duplicate == 0 else f"{base}-{duplicate}")
    return slugs


def test_relative_documentation_links_resolve() -> None:
    problems: list[str] = []
    for source in documentation_paths():
        for raw_target in MARKDOWN_LINK.findall(source.read_text(encoding="utf-8")):
            target = raw_target.strip().split()[0].strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_text, _, anchor = target.partition("#")
            destination = (source.parent / path_text).resolve()
            if not destination.exists():
                problems.append(f"{source.relative_to(ROOT)}: missing {target}")
            elif (
                anchor
                and destination.suffix.lower() == ".md"
                and anchor not in heading_slugs(destination.read_text(encoding="utf-8"))
            ):
                problems.append(f"{source.relative_to(ROOT)}: missing anchor {target}")
    assert problems == []


def test_documented_toml_is_valid_toml() -> None:
    for source in documentation_paths():
        for index, block in enumerate(
            TOML_BLOCK.findall(source.read_text(encoding="utf-8")), start=1
        ):
            try:
                tomllib.loads(block)
            except tomllib.TOMLDecodeError as exc:
                raise AssertionError(
                    f"invalid TOML in {source.relative_to(ROOT)} block {index}: {exc}"
                ) from exc


def test_general_positioning_uses_positive_complete_call_language() -> None:
    positioning_paths = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "USER_GUIDE.md",
        ROOT / "docs" / "USE_CASES.md",
        ROOT / "case_studies" / "javascript_python_rules" / "README.md",
        ROOT / "src" / "parity" / "__init__.py",
    ]
    text = re.sub(
        r"\s+",
        " ",
        "\n".join(path.read_text(encoding="utf-8").lower() for path in positioning_paths),
    )

    assert "unit of work is an explicit `callable(*args, **kwargs)` contract" in text
    assert "complete call can combine ordinary json, frames" in text
    assert "recursive-json cross-language contract" in text
    assert "behavioural compatibility verification for software migrations" in text


def section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading) + len(heading)
    return text[start : text.index(next_heading, start)]


def first_table_keys(text: str, *, after: str = "") -> set[str]:
    remaining = text[text.index(after) + len(after) :]
    keys: set[str] = set()
    in_table = False
    for line in remaining.splitlines():
        if line.startswith("|"):
            in_table = True
            if match := re.match(r"\| `([^`]+)` \|", line):
                keys.add(match.group(1))
        elif in_table and line.strip():
            break
    return keys


def test_configuration_reference_tables_cover_every_schema_field() -> None:
    text = CONFIG_REFERENCE.read_text(encoding="utf-8")
    config = json.loads((ROOT / "src/parity/schemas/config.json").read_text(encoding="utf-8"))
    workspace = json.loads((ROOT / "src/parity/schemas/workspace.json").read_text(encoding="utf-8"))
    definitions = config["$defs"]

    documented = {
        "top": first_table_keys(section(text, "## Top level", "## Reusable")),
        "workspace": first_table_keys(
            section(text, "## Migration workspace", "## Retained"),
            after="source mapping is symmetric:",
        ),
        "case": first_table_keys(section(text, "## Case", "## Invocation"), after="cases"),
        "invocation": first_table_keys(
            section(text, "## Invocation", "## Callable specification"),
            after="expanded as `*args`.",
        ),
        "frame_argument": first_table_keys(
            section(text, "## Invocation", "## Callable specification"),
            after="A `frame` accepts:",
        ),
        "json_argument": first_table_keys(
            section(text, "## Invocation", "## Callable specification"),
            after="A `json` argument accepts:",
        ),
        "frames_argument": first_table_keys(
            section(text, "## Invocation", "## Callable specification"),
            after="A `frames` argument accepts:",
        ),
        "callable": first_table_keys(
            section(text, "## Callable specification", "## Frame schema"), after="accept:"
        ),
        "frame": first_table_keys(
            section(text, "## Frame schema", "### Frame constraints"), after="accepts:"
        ),
        "column": first_table_keys(
            section(text, "## Frame schema", "### Frame constraints"), after="Declare columns"
        ),
        "comparison": first_table_keys(
            section(text, "## Comparison policy", "## Generation policy"),
            after="`[cases.comparison]`:",
        ),
        "generation": first_table_keys(
            section(text, "## Generation policy", "## Case parallelism"),
            after="`[cases.generation]`:",
        ),
        "performance": first_table_keys(
            section(text, "## Performance policy", "## Complete generated template"),
            after="`[cases.performance]`:",
        ),
    }
    actual = {
        "top": set(config["properties"]),
        "workspace": set(workspace["properties"]),
        "case": set(definitions["CaseConfig"]["properties"]),
        "invocation": set(definitions["InvocationConfig"]["properties"]),
        "frame_argument": set(definitions["FrameArgument"]["properties"]),
        "json_argument": set(definitions["JsonArgument"]["properties"]),
        "frames_argument": set(definitions["FrameSequenceArgument"]["properties"]),
        "callable": set(definitions["CallableSpec"]["properties"]),
        "frame": set(definitions["FrameSchema"]["properties"]),
        "column": set(definitions["ColumnSchema"]["properties"]),
        "comparison": set(definitions["ComparisonPolicy"]["properties"]),
        "generation": set(definitions["GenerationConfig"]["properties"]),
        "performance": set(definitions["PerformanceConfig"]["properties"]),
    }

    assert documented == actual
    assert workspace["$id"].endswith("/workspace/v3.json")
    assert "Workspace format; only version 3 is accepted." in text
