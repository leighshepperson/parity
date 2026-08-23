from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

import pyarrow as pa

from parity.config import load_config

ROOT = Path(__file__).parents[1]
STUDY = ROOT / "case_studies" / "cpp_python_orderbook"
EXPECTED_CASES = [
    "correct-port",
    "naive-port",
    "regression-lot-size",
    "regression-reference-rejects",
    "regression-partial-fill",
    "regression-candidate-rejects",
    "regression-price-priority",
]


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"orderbook_{name}", STUDY / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(
    target: Callable[[pa.Table, pa.Table], pa.Table], bundle: Mapping[str, pa.Table]
) -> tuple[str, object]:
    try:
        return "returned", target(bundle["events"], bundle["instruments"]).to_pylist()
    except Exception as error:
        return "raised", (type(error).__module__, type(error).__qualname__, str(error))


def test_cpp_python_orderbook_contract_covers_generated_and_retained_cases() -> None:
    config = load_config(STUDY / "parity.toml")
    assert [case.name for case in config.cases] == EXPECTED_CASES

    correct, naive, *regressions = config.cases
    assert correct.generation.generator == "generator:order_streams"
    assert correct.generation.max_examples == 750
    assert correct.performance.enabled
    assert correct.performance.fail_on_regression is False
    assert naive.generation.generator == "generator:order_streams"
    assert naive.generation.max_examples == 500
    assert naive.generation.max_findings == 6
    assert not naive.performance.enabled
    assert all(case.generation.max_examples == 1 for case in regressions)
    assert all(case.generation.stability_repeats == 3 for case in regressions)
    assert all("regression" in case.tags for case in regressions)

    for case in config.cases:
        assert case.reference.command == [
            "parity",
            "adapter",
            "serve",
            "reference_adapter.py",
        ]
        assert case.reference.workdir == STUDY
        assert case.candidate.workdir == STUDY
        assert case.comparison.rtol == 0
        assert case.comparison.atol == 0
        assert case.comparison.row_order == "strict"
        assert case.comparison.dtype == "strict"


def test_cpp_python_orderbook_retained_streams_expose_all_injected_defects() -> None:
    candidate = _module("candidate")
    generator = _module("generator")
    factories = (
        generator.regression_lot_size,
        generator.regression_reference_rejects,
        generator.regression_partial_fill,
        generator.regression_candidate_rejects,
        generator.regression_price_priority,
    )

    outcomes = []
    row_counts = []
    for factory in factories:
        invocation = next(iter(factory()))
        bundle = invocation.kwargs
        correct = _capture(candidate.correct_port, bundle)
        naive = _capture(candidate.naive_port, bundle)
        assert correct != naive
        outcomes.append((correct[0], naive[0]))
        row_counts.append(bundle["events"].num_rows)

    assert row_counts == [2, 7, 10, 7, 3]
    assert outcomes == [
        ("returned", "returned"),
        ("raised", "returned"),
        ("returned", "returned"),
        ("returned", "raised"),
        ("returned", "returned"),
    ]


def test_cpp_python_orderbook_sources_and_ci_entrypoints_are_present() -> None:
    for name in ("candidate.py", "generator.py", "reference_adapter.py", "soak.py", "verify.py"):
        source = (STUDY / name).read_text(encoding="utf-8")
        compile(source, str(STUDY / name), "exec")

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    soak = (ROOT / ".github" / "workflows" / "cross-language-soak.yml").read_text(encoding="utf-8")
    assert "cpp-python-orderbook:" in workflow
    assert "verify.py --profile quick" in workflow
    assert "schedule:" in soak
    assert "workflow_dispatch:" in soak
    assert "verify.py --profile full" in soak
    assert 'python soak.py --calls "$SOAK_CALLS"' in soak
    assert 'default: "2000"' in soak
