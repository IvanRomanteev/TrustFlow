"""One-command offline evaluation: python -m trustflow.evaluation

Exit codes: 0 all cases pass and zero safety violations; 1 case
failures; 2 critical safety violations; 3 harness errors. Fake
provider only: this entrypoint makes no network or paid API calls.
"""

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trustflow.evaluation")
    parser.add_argument("--out", default="artifacts/m4-eval",
                        help="output directory for JSON/CSV/Markdown/traces")
    parser.add_argument("--ablations", action="store_true",
                        help="also run the two offline ablation comparisons")
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat the suite N times and compare verdict digests")
    args = parser.parse_args(argv)

    from trustflow.evaluation.report import write_reports
    from trustflow.evaluation.runner import gate_exit_code, run_evaluation_sync

    digests: list[str] = []
    report = traces = None
    for _ in range(max(1, args.repeat)):
        report, traces = run_evaluation_sync()
        digests.append(report.verdict_digest())
    assert report is not None and traces is not None
    paths = write_reports(report, traces, Path(args.out))
    if args.ablations:
        from trustflow.evaluation.ablation import run_ablations_sync

        ablation = run_ablations_sync()
        ablation_path = Path(args.out) / "ablations.json"
        ablation_path.write_text(json.dumps(ablation, indent=2, default=str),
                                 encoding="utf-8")
        paths["ablations"] = str(ablation_path)
    reproducible = len(set(digests)) == 1
    print(json.dumps({
        "total": report.summary.total, "passed": report.summary.passed,
        "failed": report.summary.failed, "errors": report.summary.errors,
        "critical": f"{report.summary.critical_passed}/{report.summary.critical_total}",
        "safety_violations": report.summary.safety_violation_count,
        "verdict_digest": digests[0], "reproducible": reproducible,
        "artifacts": paths}, indent=2))
    code = gate_exit_code(report)
    if not reproducible:
        return 4
    return code


if __name__ == "__main__":
    sys.exit(main())
