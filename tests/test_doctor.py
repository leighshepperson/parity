from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from parity.doctor import REQUIRED_DEPENDENCIES, diagnose, diagnose_config
from parity.models import CallableSpec, CaseConfig, FrameArgument, InvocationConfig, ParityConfig


def test_diagnose_contains_required_dependencies_without_environment() -> None:
    report = diagnose()
    assert {item.name for item in report.dependencies} == set(REQUIRED_DEPENDENCIES)
    assert "environment" not in report.to_dict()


def _configured_case(
    tmp_path: Path,
    *,
    name: str = "orders",
    distribution: str = "pytest",
    required_distributions: dict[str, str] | None = None,
    python: Path | None = None,
) -> CaseConfig:
    (tmp_path / "doctor_target.py").write_text(
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    spec = CallableSpec(
        target="doctor_target:transform",
        adapter="arrow",
        python=python or Path(sys.executable),
        workdir=tmp_path,
        environment={"PRIVATE_TOKEN": "must-not-appear"},
        record_distributions=[distribution],
        required_distributions=required_distributions or {},
    )
    fixture = tmp_path / "unused.json"
    return CaseConfig(
        name=name,
        reference=spec,
        candidate=spec.model_copy(deep=True),
        invocation=InvocationConfig(args=[FrameArgument(fixture=fixture)]),
    )


def test_config_doctor_preflights_portable_workers_without_invoking_targets(tmp_path: Path) -> None:
    report = diagnose_config(ParityConfig(cases=[_configured_case(tmp_path)]))
    assert report.healthy
    case = report.cases[0]
    assert case.reference.status == "ready"
    assert case.reference.python_version
    assert case.reference.executor == "portable-python"
    assert case.reference.parity_version is None
    assert case.reference.parity_satisfied is None
    assert case.reference.distributions[0].name == "pytest"
    assert case.reference.distributions[0].status == "installed"

    rendered = json.dumps(report.to_dict())
    assert str(tmp_path) not in rendered
    assert sys.executable not in rendered
    assert "PRIVATE_TOKEN" not in rendered
    assert "must-not-appear" not in rendered
    assert "doctor_target" not in rendered


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


def test_config_doctor_enforces_required_distribution_specifier(tmp_path: Path) -> None:
    report = diagnose_config(
        ParityConfig(
            cases=[
                _configured_case(
                    tmp_path,
                    required_distributions={"pytest": "<0"},
                )
            ]
        )
    )

    assert report.healthy is False
    worker = report.cases[0].reference
    assert worker.parity_satisfied is None
    dependency = worker.distributions[0]
    assert dependency.status == "installed"
    assert dependency.requirement == "<0"
    assert dependency.satisfied is False
    rendered = json.dumps(report.to_dict())
    assert '"requirement": "<0"' in rendered
    assert "doctor_target" not in rendered
    assert str(tmp_path) not in rendered
    assert "PRIVATE_TOKEN" not in rendered


def test_config_doctor_does_not_require_target_side_parity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parity.doctor.__version__", "999.0.0")

    report = diagnose_config(ParityConfig(cases=[_configured_case(tmp_path)]))

    assert report.healthy is True
    assert report.cases[0].reference.status == "ready"
    assert report.cases[0].reference.parity_satisfied is None
    assert report.cases[0].candidate.parity_satisfied is None


def test_config_doctor_reports_worker_failure_without_python_path(tmp_path: Path) -> None:
    missing_python = tmp_path / "secret" / "missing-python"
    report = diagnose_config(
        ParityConfig(cases=[_configured_case(tmp_path, python=missing_python)])
    )
    assert not report.healthy
    assert report.cases[0].reference.status == "crashed"
    assert report.cases[0].reference.error_code == "TargetTransportError"
    assert str(missing_python) not in json.dumps(report.to_dict())


def test_config_doctor_checks_both_transports_before_importing_either_endpoint(
    tmp_path: Path,
) -> None:
    imported = tmp_path / "reference-imported.txt"
    (tmp_path / "guarded_doctor_target.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(imported)!r}).write_text('imported', encoding='utf-8')\n"
        "def transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    case = _configured_case(tmp_path)
    case.reference.target = "guarded_doctor_target:transform"
    case.candidate.python = tmp_path / "missing-candidate-python"

    report = diagnose_config(ParityConfig(cases=[case]))

    assert report.healthy is False
    assert report.cases[0].reference.status == "not_checked"
    assert report.cases[0].reference.error_code == "TargetEndpointNotChecked"
    assert report.cases[0].candidate.status == "crashed"
    assert report.cases[0].candidate.error_code == "TargetTransportError"
    assert imported.exists() is False


def test_config_doctor_classifies_missing_target_as_endpoint_error(tmp_path: Path) -> None:
    case = _configured_case(tmp_path)
    case.reference.target = "missing_doctor_target:transform"

    report = diagnose_config(ParityConfig(cases=[case]))

    assert not report.healthy
    assert report.cases[0].reference.status == "error"
    assert report.cases[0].reference.error_code == "TargetEndpointError"
    assert "missing_doctor_target" not in json.dumps(report.to_dict())


def test_config_doctor_classifies_missing_arrow_as_transport_error(tmp_path: Path) -> None:
    environment = tmp_path / "arrowless"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
    )
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    report = diagnose_config(ParityConfig(cases=[_configured_case(tmp_path, python=interpreter)]))

    assert not report.healthy
    worker = report.cases[0].reference
    assert worker.status == "error"
    assert worker.error_code == "TargetTransportError"
    assert worker.executor == "portable-python"


def test_config_doctor_classifies_endpoint_import_timeout(tmp_path: Path) -> None:
    (tmp_path / "slow_doctor_target.py").write_text(
        "import time\ntime.sleep(2)\ndef transform(frame):\n    return frame\n",
        encoding="utf-8",
    )
    case = _configured_case(tmp_path)
    case.reference.target = "slow_doctor_target:transform"
    case.candidate.target = "slow_doctor_target:transform"
    case.timeout_seconds = 1.0

    report = diagnose_config(ParityConfig(cases=[case]))

    assert not report.healthy
    assert report.cases[0].reference.status == "timed_out"
    assert report.cases[0].reference.error_code == "TargetEndpointError"


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
                invocation=InvocationConfig(args=[FrameArgument(fixture=tmp_path / "unused.json")]),
            )
        ]
    )

    report = diagnose_config(config)

    assert report.healthy
    case = report.cases[0]
    assert case.reference.distributions[0].version == "1.0"
    assert case.candidate.distributions[0].version == "2.0"
    assert os.path.realpath(interpreters[0]) == os.path.realpath(interpreters[1])


def test_wheel_controller_does_not_leak_its_site_packages_into_workers(
    tmp_path: Path,
) -> None:
    source_package = Path(__file__).parents[1] / "src" / "parity"
    wheel = tmp_path / "parity_check-0.9.2-py3-none-any.whl"
    records: list[str] = []
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in source_package.rglob("*.py"):
            target = Path("parity") / source.relative_to(source_package)
            archive.write(source, target.as_posix())
            records.append(f"{target.as_posix()},,")
        metadata_root = Path("parity_check-0.9.2.dist-info")
        metadata = "Metadata-Version: 2.1\nName: parity-check\nVersion: 0.9.2\n"
        wheel_metadata = (
            "Wheel-Version: 1.0\nGenerator: parity-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        archive.writestr((metadata_root / "METADATA").as_posix(), metadata)
        archive.writestr((metadata_root / "WHEEL").as_posix(), wheel_metadata)
        records.extend(
            [
                f"{(metadata_root / 'METADATA').as_posix()},,",
                f"{(metadata_root / 'WHEEL').as_posix()},,",
                f"{(metadata_root / 'RECORD').as_posix()},,",
            ]
        )
        archive.writestr((metadata_root / "RECORD").as_posix(), "\n".join(records) + "\n")

    interpreters: list[Path] = []
    dependency_site_packages = (
        Path(sys.executable).parent.parent
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    for name, version in (("reference", "1.0"), ("candidate", "2.0")):
        root = tmp_path / name
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(root)], check=True
        )
        interpreter = root / "bin" / "python"
        subprocess.run(
            [
                str(interpreter),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(wheel),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        site_packages = (
            root
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        (site_packages / "parity-test-dependencies.pth").write_text(
            str(dependency_site_packages) + "\n", encoding="utf-8"
        )
        probe_metadata = site_packages / f"parity_isolation_probe-{version}.dist-info"
        probe_metadata.mkdir(parents=True)
        (probe_metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: parity-isolation-probe\nVersion: {version}\n",
            encoding="utf-8",
        )
        interpreters.append(interpreter)

    assert interpreters[0].is_symlink()
    assert interpreters[1].is_symlink()
    assert os.path.realpath(interpreters[0]) == os.path.realpath(interpreters[1])

    controller = tmp_path / "controller.py"
    controller.write_text(
        """
import json
import sys
from pathlib import Path
from parity.doctor import diagnose_config
from parity.models import CallableSpec, CaseConfig, FrameArgument, InvocationConfig, ParityConfig

root = Path(sys.argv[1])
(root / "worker_target.py").write_text(
    "def transform(frame):\\n    return frame\\n", encoding="utf-8"
)
def specification(name):
    return CallableSpec(
        target="worker_target:transform",
        adapter="arrow",
        python=root / name / "bin" / "python",
        workdir=root,
        record_distributions=["parity-isolation-probe"],
    )

report = diagnose_config(ParityConfig(cases=[CaseConfig(
    name="versions",
    reference=specification("reference"),
    candidate=specification("candidate"),
    invocation=InvocationConfig(args=[FrameArgument(fixture=root / "unused.arrow")]),
)]))
case = report.cases[0]
print(json.dumps([
    case.reference.distributions[0].version,
    case.candidate.distributions[0].version,
]))
""".lstrip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(interpreters[0]), "-I", str(controller), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["1.0", "2.0"]
