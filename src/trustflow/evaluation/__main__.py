"""One-command offline evaluation: python -m trustflow.evaluation

Lifecycle (F5): validate inputs -> execute all repeats -> execute
ablations -> write every artifact into a staging directory ->
validate the staged JSON -> publish by rename -> write the completion
marker as part of the staged set. A failed stage removes the staging
directory and never touches a previously published successful report;
no PASS artifact is published for an incomplete run.

Exit-code precedence (highest first), with all findings preserved in
the published repeats.json / COMPLETE.json and in stdout:
  2  critical safety violation in any repeat
  3  harness/infrastructure failure (including write failures and
     per-case harness errors)
  4  reproducibility mismatch across repeats
  1  evaluated case assertion failure
  0  valid, completed, successful gate
 64  command-line input error (invalid arguments); this is a usage
     error, distinct from every evaluation outcome above.

Fake provider only: no network or paid API calls. Errors are printed
sanitized and bounded; tracebacks are never published (F4).
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

EXIT_USAGE = 64


def _fail(message: str, code: int) -> int:
    print(json.dumps({"error": message}), file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trustflow.evaluation")
    parser.add_argument("--out", default="artifacts/m4-eval",
                        help="output directory for JSON/CSV/Markdown/traces")
    parser.add_argument("--ablations", action="store_true",
                        help="also run the two offline ablation comparisons")
    parser.add_argument("--repeat", default="1",
                        help="repeat the suite N times and compare verdict digests")
    args = parser.parse_args(argv)
    try:
        repeat = int(args.repeat)
    except (TypeError, ValueError):
        return _fail("--repeat must be a positive integer", EXIT_USAGE)
    if repeat < 1:
        return _fail("--repeat must be a positive integer", EXIT_USAGE)

    from trustflow.evaluation import ablation as ablation_module
    from trustflow.evaluation import report as report_module
    from trustflow.evaluation import runner as runner_module
    from trustflow.evaluation.errors import safe_error_detail

    out = Path(args.out)
    staging = out.with_name(f"{out.name}.staging-{os.getpid()}")
    backup = out.with_name(f"{out.name}.previous-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        reports = []
        traces: dict = {}
        for _ in range(repeat):
            report, traces = runner_module.run_evaluation_sync()
            reports.append(report)
        digests = [r.verdict_digest() for r in reports]
        paths = report_module.write_reports(reports[-1], traces, staging)
        repeats_payload = {
            "repeats": [
                {"index": i, "total": r.summary.total,
                 "passed": r.summary.passed, "failed": r.summary.failed,
                 "errors": r.summary.errors,
                 "safety_violations": r.summary.safety_violation_count,
                 "verdict_digest": d}
                for i, (r, d) in enumerate(zip(reports, digests, strict=True))],
            "reproducible": len(set(digests)) == 1,
        }
        repeats_path = staging / "repeats.json"
        report_module._atomic_write(
            repeats_path, json.dumps(repeats_payload, indent=2))
        paths["repeats"] = str(repeats_path)
        if args.ablations:
            ablation = ablation_module.run_ablations_sync()
            ablation_path = staging / "ablations.json"
            report_module._atomic_write(
                ablation_path, json.dumps(ablation, indent=2, default=str))
            paths["ablations"] = str(ablation_path)
        for staged in staging.glob("*.json"):
            json.loads(staged.read_text(encoding="utf-8"))
        safety_total = sum(r.summary.safety_violation_count for r in reports)
        marker = {
            "complete": True,
            "verdict_digests": digests,
            "reproducible": len(set(digests)) == 1,
            "safety_violations_total": safety_total,
            "config_digest": reports[-1].run_metadata.config_digest,
            "dataset_digest": reports[-1].run_metadata.dataset_digest,
        }
        report_module._atomic_write(
            staging / "COMPLETE.json", json.dumps(marker, indent=2))
        # Publish only now that every required artifact succeeded.
        if out.exists():
            os.rename(out, backup)
            try:
                os.rename(staging, out)
            except Exception:
                os.rename(backup, out)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        else:
            os.rename(staging, out)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return _fail(safe_error_detail(exc), 3)

    published = {name: str(out / Path(str(path)).name)
                 for name, path in paths.items()}
    print(json.dumps({
        "total": reports[-1].summary.total,
        "passed": reports[-1].summary.passed,
        "failed": reports[-1].summary.failed,
        "errors": reports[-1].summary.errors,
        "safety_violations": safety_total,
        "repeats": repeats_payload["repeats"],
        "verdict_digest": digests[-1],
        "reproducible": len(set(digests)) == 1,
        "config_digest": reports[-1].run_metadata.config_digest,
        "artifacts": published}, indent=2))
    if safety_total:
        return 2
    if any(r.summary.errors for r in reports):
        return 3
    if len(set(digests)) != 1:
        return 4
    if any(r.summary.failed for r in reports):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
