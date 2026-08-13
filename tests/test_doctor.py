from parity.doctor import REQUIRED_DEPENDENCIES, diagnose


def test_diagnose_contains_required_dependencies_without_environment() -> None:
    report = diagnose()
    assert {item.name for item in report.dependencies} == set(REQUIRED_DEPENDENCIES)
    assert "environment" not in report.to_dict()
