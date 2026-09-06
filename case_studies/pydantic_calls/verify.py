"""Exercise the direct comparison API on real, documented dependency changes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from parity import compare, verify_evidence
from parity.models import CallableSpec, Status
from parity.reporting import write_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    artifacts = root / ".parity"
    reference = CallableSpec(
        target="order_contract:validate_order",
        workdir=root,
        python=args.reference_python,
        record_distributions=["pydantic"],
        required_distributions={"pydantic": "<2"},
    )
    candidate = reference.model_copy(
        update={"python": args.candidate_python, "required_distributions": {"pydantic": ">=2"}}
    )
    control = compare(reference, candidate, calls=root / "control.jsonl", artifact_dir=artifacts)
    if not control.passed:
        raise SystemExit("control did not pass")
    result = compare(reference, candidate, calls=root / "calls.jsonl", artifact_dir=artifacts)
    case = result.cases[0]
    if result.status is not Status.FAILED or len(case.failures) != 4:
        raise SystemExit(f"expected four differences, got {result.status}: {len(case.failures)}")
    if case.examples_run != 5 or case.generated_examples != 0:
        raise SystemExit("the supplied calls were not executed exactly")
    report = write_report(result, "json", artifacts / "report.json")
    evidence = verify_evidence(report, artifact_root=artifacts)
    if evidence.status is not Status.PASSED:
        raise SystemExit("retained findings did not replay")
    print(
        json.dumps(
            {
                "control": control.status.value,
                "migration": result.status.value,
                "calls": case.examples_run,
                "findings": len(case.failures),
                "replay": "passed",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
