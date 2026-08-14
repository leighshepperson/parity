from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from parity.migration import (
    MigrationCaseStatus,
    MigrationConfigError,
    MigrationManifest,
    MigrationUnit,
    MigrationUnitStatus,
    check_migration,
    load_migration_manifest,
    migration_manifest_sha256,
    migration_report_payload,
    render_migration_json,
    run_migration,
    write_migration_json,
)
from parity.models import (
    CallableSpec,
    CaseConfig,
    CaseResult,
    ColumnSchema,
    ExampleResult,
    FrameSchema,
    GenerationConfig,
    Mismatch,
    MismatchKind,
    ParityConfig,
    PerformanceConfig,
    Status,
    SuiteProvenance,
    SuiteResult,
)
from parity.provenance import collect_runtime_provenance


def _config(*names: str, fail_fast: bool = False) -> ParityConfig:
    schema = FrameSchema(columns=[ColumnSchema(name="value", dtype="integer")])
    return ParityConfig(
        fail_fast=fail_fast,
        cases=[
            CaseConfig(
                name=name,
                reference=CallableSpec(target="example:reference"),
                candidate=CallableSpec(target="example:candidate"),
                input_schema=schema,
            )
            for name in names
        ],
    )


def _case(name: str, status: Status, *, examples: int = 1) -> CaseResult:
    return CaseResult(name=name, status=status, examples_run=examples)


def _suite(*cases: CaseResult, status: Status | None = None) -> SuiteResult:
    derived = status
    if derived is None:
        if any(case.status is Status.ERROR for case in cases):
            derived = Status.ERROR
        elif any(case.status is Status.FAILED for case in cases):
            derived = Status.FAILED
        else:
            derived = Status.PASSED
    return SuiteResult(
        status=derived,
        cases=list(cases),
        provenance=SuiteProvenance(
            orchestrator=collect_runtime_provenance(),
            config_sha256="a" * 64,
        ),
    )


def test_manifest_loader_normalizes_and_hashes_effective_contract(tmp_path: Path) -> None:
    path = tmp_path / "migration.toml"
    path.write_text(
        """
version = 1

[[units]]
id = "orders"
cases = ["orders-control", "orders-null"]

[[units]]
id = "plotting"
excluded_reason = "  Returns a figure.  "

[[units]]
id = "unfinished"
""",
        encoding="utf-8",
    )

    manifest = load_migration_manifest(path)

    assert manifest.units[1].excluded_reason == "Returns a figure."
    assert manifest.units[2].cases == []
    assert migration_manifest_sha256(manifest) == migration_manifest_sha256(
        MigrationManifest.model_validate(manifest.model_dump(mode="json"))
    )
    assert len(migration_manifest_sha256(manifest)) == 64


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            {
                "units": [
                    {"id": "orders", "cases": ["control"]},
                    {"id": "orders", "cases": ["edge"]},
                ]
            },
            "unit ids must be unique",
        ),
        (
            {"units": [{"id": "orders", "cases": ["control", "control"]}]},
            "unit cases must be unique",
        ),
        (
            {
                "units": [
                    {
                        "id": "orders",
                        "cases": ["control"],
                        "excluded_reason": "not supported",
                    }
                ]
            },
            "both cases and excluded_reason",
        ),
        (
            {"units": [{"id": "orders", "excluded_reason": "   "}]},
            "cannot be blank",
        ),
        ({"units": [{"id": "orders", "unknown": True}]}, "Extra inputs"),
    ],
)
def test_manifest_rejects_ambiguous_or_misspelled_entries(
    raw: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        MigrationManifest.model_validate(raw)


def test_manifest_loader_reports_missing_and_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(MigrationConfigError, match="manifest not found"):
        load_migration_manifest(tmp_path / "missing.toml")

    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[[", encoding="utf-8")
    with pytest.raises(MigrationConfigError, match="invalid TOML"):
        load_migration_manifest(invalid)


def test_run_migration_executes_shared_union_once_and_disables_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = MigrationManifest(
        units=[
            MigrationUnit(id="orders", cases=["control", "edge"]),
            MigrationUnit(id="customers", cases=["control"]),
            MigrationUnit(id="plotting", excluded_reason="figure output"),
        ]
    )
    config = _config("control", "edge", "unmapped", fail_fast=True)
    calls: list[tuple[ParityConfig, set[str] | None]] = []

    def fake_run_suite(
        selected_config: ParityConfig,
        *,
        selected_cases: set[str] | None = None,
    ) -> SuiteResult:
        calls.append((selected_config, selected_cases))
        return _suite(_case("control", Status.PASSED), _case("edge", Status.PASSED))

    monkeypatch.setattr("parity.engine.run_suite", fake_run_suite)

    result = run_migration(manifest, config)

    assert result.status is Status.PASSED
    assert result.passed
    assert [unit.status for unit in result.units] == [
        MigrationUnitStatus.PASSED,
        MigrationUnitStatus.PASSED,
        MigrationUnitStatus.EXCLUDED,
    ]
    assert len(calls) == 1
    effective, selected = calls[0]
    assert selected == {"control", "edge"}
    assert effective is not config
    assert effective.fail_fast is False
    assert config.fail_fast is True


def test_unknown_mapped_case_fails_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_run_suite(*_args: object, **_kwargs: object) -> SuiteResult:
        nonlocal called
        called = True
        return _suite()

    monkeypatch.setattr("parity.engine.run_suite", fake_run_suite)
    manifest = MigrationManifest(units=[MigrationUnit(id="orders", cases=["missing"])])

    with pytest.raises(MigrationConfigError, match=r"unknown case.*missing"):
        run_migration(manifest, _config("control"))
    assert not called


@pytest.mark.parametrize(
    ("case_result", "case_status", "suite_status"),
    [
        (None, MigrationCaseStatus.MISSING, Status.PASSED),
        (_case("control", Status.SKIPPED), MigrationCaseStatus.SKIPPED, Status.PASSED),
        (
            _case("control", Status.PASSED, examples=0),
            MigrationCaseStatus.NOT_EXERCISED,
            Status.PASSED,
        ),
        (_case("control", Status.ERROR), MigrationCaseStatus.ERROR, Status.ERROR),
    ],
)
def test_incomplete_or_error_case_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    case_result: CaseResult | None,
    case_status: MigrationCaseStatus,
    suite_status: Status,
) -> None:
    cases = () if case_result is None else (case_result,)
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(*cases, status=suite_status),
    )

    result = run_migration(
        MigrationManifest(units=[MigrationUnit(id="orders", cases=["control"])]),
        _config("control"),
    )

    assert result.status is Status.ERROR
    assert result.units[0].status is MigrationUnitStatus.ERROR
    assert result.units[0].cases[0].status is case_status


def test_semantic_failure_and_uncovered_units_are_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(_case("control", Status.FAILED)),
    )
    result = run_migration(
        MigrationManifest(
            units=[
                MigrationUnit(id="orders", cases=["control"]),
                MigrationUnit(id="customers"),
            ]
        ),
        _config("control"),
    )

    assert result.status is Status.FAILED
    assert [unit.status for unit in result.units] == [
        MigrationUnitStatus.FAILED,
        MigrationUnitStatus.UNCOVERED,
    ]


@pytest.mark.parametrize("kind", ["excluded", "uncovered"])
def test_empty_mapped_union_never_runs_all_cases_and_cannot_pass(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    selected_values: list[set[str] | None] = []

    def fake_run_suite(
        _config: ParityConfig,
        *,
        selected_cases: set[str] | None = None,
    ) -> SuiteResult:
        selected_values.append(selected_cases)
        return _suite()

    monkeypatch.setattr("parity.engine.run_suite", fake_run_suite)
    unit = (
        MigrationUnit(id="plotting", excluded_reason="figure output")
        if kind == "excluded"
        else MigrationUnit(id="orders")
    )

    result = run_migration(MigrationManifest(units=[unit]), _config("unrelated"))

    assert result.status is Status.FAILED
    assert selected_values == [set()]
    expected = MigrationUnitStatus.EXCLUDED if kind == "excluded" else MigrationUnitStatus.UNCOVERED
    assert result.units[0].status is expected


def test_error_takes_precedence_over_semantic_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(
            _case("failed", Status.FAILED),
            _case("errored", Status.ERROR),
        ),
    )
    result = run_migration(
        MigrationManifest(
            units=[
                MigrationUnit(id="one", cases=["failed"]),
                MigrationUnit(id="two", cases=["errored"]),
            ]
        ),
        _config("failed", "errored"),
    )

    assert result.status is Status.ERROR


def test_report_is_data_eliding_redacted_and_bound_to_both_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_reference = "reference-cell-secret"
    secret_candidate = "candidate-cell-secret"
    failure = ExampleResult(
        source="fixture",
        status=Status.FAILED,
        mismatches=[
            Mismatch(
                kind=MismatchKind.VALUE,
                message="values differ",
                reference=secret_reference,
                candidate=secret_candidate,
            )
        ],
    )
    case = _case("control", Status.FAILED)
    case.failures = [failure]
    monkeypatch.setattr("parity.engine.run_suite", lambda *_args, **_kwargs: _suite(case))
    result = run_migration(
        MigrationManifest(
            units=[
                MigrationUnit(id="orders", cases=["control"]),
                MigrationUnit(
                    id="plotting",
                    excluded_reason="Private fixture at /private/project/data.csv",
                ),
            ]
        ),
        _config("control"),
    )

    payload = migration_report_payload(result)
    encoded = json.dumps(payload)

    assert payload["schema_version"] == 1
    assert payload["manifest_sha256"] == result.manifest_sha256
    assert payload["parity"]["provenance"]["config_sha256"] == "a" * 64
    assert payload["summary"] == {
        "total": 2,
        "passed": 0,
        "failed": 1,
        "error": 0,
        "excluded": 1,
        "uncovered": 0,
    }
    assert "<path>" in payload["units"][1]["excluded_reason"]
    assert secret_reference not in encoded
    assert secret_candidate not in encoded
    assert "mismatch_counts" in encoded


def test_json_writer_is_atomic_shape_compatible_and_replaces_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(_case("control", Status.PASSED)),
    )
    checked = run_migration(
        MigrationManifest(units=[MigrationUnit(id="orders", cases=["control"])]),
        _config("control"),
    )

    output = tmp_path / "nested" / "migration.json"
    output.parent.mkdir()
    output.write_text("old", encoding="utf-8")

    written = write_migration_json(checked, output)

    assert written == output
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"
    assert render_migration_json(checked).endswith("\n")


def test_check_migration_loads_both_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest_path = project / "migration.toml"
    manifest_path.write_text(
        'version = 1\n[[units]]\nid = "orders"\ncases = ["control"]\n',
        encoding="utf-8",
    )
    config_path = project / "parity.toml"
    config_path.write_text(
        """
version = 1
[[cases]]
name = "control"
[cases.reference]
target = "example:reference"
[cases.candidate]
target = "example:candidate"
[cases.schema]
[[cases.schema.columns]]
name = "value"
dtype = "integer"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "parity.engine.run_suite",
        lambda *_args, **_kwargs: _suite(_case("control", Status.PASSED)),
    )

    assert check_migration(manifest_path, config_path).passed


def test_run_migration_exercises_real_isolated_workers(tmp_path: Path) -> None:
    (tmp_path / "migration_target.py").write_text(
        "def identity(frame):\n    return frame\n",
        encoding="utf-8",
    )
    callable_spec = CallableSpec(
        target="migration_target:identity",
        adapter="arrow",
        workdir=tmp_path,
    )
    config = ParityConfig(
        artifact_dir=tmp_path / ".parity",
        cases=[
            CaseConfig(
                name="identity",
                reference=callable_spec,
                candidate=callable_spec.model_copy(deep=True),
                input_schema=FrameSchema(
                    min_rows=1,
                    max_rows=1,
                    columns=[ColumnSchema(name="value", dtype="integer")],
                ),
                generation=GenerationConfig(
                    max_examples=1,
                    stability_repeats=1,
                    adversarial_examples=False,
                    derandomize=True,
                ),
                performance=PerformanceConfig(enabled=False),
            )
        ],
    )

    result = run_migration(
        MigrationManifest(units=[MigrationUnit(id="identity-api", cases=["identity"])]),
        config,
    )

    assert result.status is Status.PASSED
    assert result.units[0].status is MigrationUnitStatus.PASSED
    assert result.units[0].cases[0].examples_run >= 1
