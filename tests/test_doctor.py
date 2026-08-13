from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from parity.doctor import REQUIRED_DEPENDENCIES, diagnose, diagnose_config
from parity.models import CallableSpec, CaseConfig, ParityConfig


def test_diagnose_contains_required_dependencies_without_environment() -> None:
    report = diagnose()
    assert {item.name for item in report.dependencies} == set(REQUIRED_DEPENDENCIES)
    assert "environment" not in report.to_dict()


def _configured_case(
    tmp_path: Path,
    *,
    name: str = "orders",
    distribution: str = "pytest",
    python: Path | None = None,
) -> CaseConfig:
    spec = CallableSpec(
        target="module.that.does.not.exist:transform",
        adapter="arrow",
        python=python or Path(sys.executable),
        workdir=tmp_path,
        environment={"PRIVATE_TOKEN": "must-not-appear"},
        record_distributions=[distribution],
    )
    fixture = tmp_path / "unused.json"
    return CaseConfig(
        name=name,
        reference=spec,
        candidate=spec.model_copy(deep=True),
        fixture=fixture,
    )


def test_config_doctor_inspects_workers_without_importing_targets(tmp_path: Path) -> None:
    report = diagnose_config(ParityConfig(cases=[_configured_case(tmp_path)]))
    assert report.healthy
    case = report.cases[0]
    assert case.reference.status == "ready"
    assert case.reference.python_version
    assert case.reference.parity_version == "0.8.0"
    assert case.reference.distributions[0].name == "pytest"
    assert case.reference.distributions[0].status == "installed"

    rendered = json.dumps(report.to_dict())
    assert str(tmp_path) not in rendered
    assert sys.executable not in rendered
    assert "PRIVATE_TOKEN" not in rendered
    assert "must-not-appear" not in rendered
    assert "module.that.does.not.exist" not in rendered


def test_config_doctor_fails_when_explicit_distribution_is_missing(tmp_path: Path) -> None:
    report = diagnose_config(
        ParityConfig(
            cases=[_configured_case(tmp_path, distribution="parity-package-does-not-exist")]
        )
    )
    assert not report.healthy
    dependency = report.cases[0].reference.distributions[0]
    assert dependency.status == "missing"
    assert dependency.version is None


def test_config_doctor_reports_worker_failure_without_python_path(tmp_path: Path) -> None:
    missing_python = tmp_path / "secret" / "missing-python"
    report = diagnose_config(
        ParityConfig(cases=[_configured_case(tmp_path, python=missing_python)])
    )
    assert not report.healthy
    assert report.cases[0].reference.status == "crashed"
    assert str(missing_python) not in json.dumps(report.to_dict())


def test_config_doctor_filters_case_names(tmp_path: Path) -> None:
    config = ParityConfig(
        cases=[
            _configured_case(tmp_path, name="orders"),
            _configured_case(tmp_path, name="customers"),
        ]
    )
    report = diagnose_config(config, case_name="customers")
    assert [case.name for case in report.cases] == ["customers"]


def test_config_doctor_uses_distinct_virtualenv_symlinks_and_site_packages(
    tmp_path: Path,
) -> None:
    site_roots: list[Path] = []
    interpreters: list[Path] = []
    for name, version in (("old", "1.0"), ("new", "2.0")):
        root = tmp_path / name
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(root)], check=True
        )
        interpreter = root / "bin" / "python"
        site_packages = (
            root
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        metadata = site_packages / "parity_probe-1.0.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: parity-probe\nVersion: {version}\n",
            encoding="utf-8",
        )
        interpreters.append(interpreter)
        site_roots.append(site_packages)

    source_site_packages = (
        Path(sys.executable).parent.parent
        / "lib"
        / (f"python{sys.version_info.major}.{sys.version_info.minor}")
        / "site-packages"
    )

    reference = _configured_case(
        tmp_path, distribution="parity-probe", python=interpreters[0]
    ).reference
    candidate = reference.model_copy(
        update={
            "python": interpreters[1],
            "environment": {
                "PYTHONPATH": os.pathsep.join((str(site_roots[1]), str(source_site_packages)))
            },
        },
        deep=True,
    )
    reference.environment = {
        "PYTHONPATH": os.pathsep.join((str(site_roots[0]), str(source_site_packages)))
    }
    config = ParityConfig(
        cases=[
            CaseConfig(
                name="versions",
                reference=reference,
                candidate=candidate,
                fixture=tmp_path / "unused.json",
            )
        ]
    )

    report = diagnose_config(config)

    assert report.healthy
    case = report.cases[0]
    assert case.reference.distributions[0].version == "1.0"
    assert case.candidate.distributions[0].version == "2.0"
    assert os.path.realpath(interpreters[0]) == os.path.realpath(interpreters[1])
