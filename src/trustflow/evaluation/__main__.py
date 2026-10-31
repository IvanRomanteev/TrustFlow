"""One-command offline evaluation: python -m trustflow.evaluation

Lifecycle: validate inputs -> execute all repeats, accumulating
safety findings as each repeat completes -> execute ablations ->
compute the gate outcome -> sanitize and write every artifact into a
private staging directory -> validate staged JSON -> publish by
rename -> COMPLETE.json (part of the staged set) marks a fully
published attempt; it is not by itself an evaluation PASS.

Exit-code precedence (highest first), applied identically on the
success path and in the exception handler, so an infrastructure
failure can never erase an already observed safety violation:
  2  critical safety violation in any completed repeat
  3  harness/infrastructure failure
  4  reproducibility mismatch across repeats
  1  evaluated case assertion failure
  0  valid, completed, successful gate
 64  command-line input error (invalid arguments).

Preservation: when the new attempt's gate is not a clean pass and the
output directory already holds a completed publication, the failed
attempt is published to a separate ``<name>.failed-<id>`` directory
and the previous successful publication is left untouched. Staging
directories are unique to each attempt and only ever removed by their
owner. Errors are printed sanitized and bounded; tracebacks are never
published (F4). Fake provider only: no network or paid API calls.
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

EXIT_USAGE = 64


def _fail(message: str, code: int, extra: dict | None = None) -> int:
    payload = {"error": message, **(extra or {})}
    print(json.dumps(payload), file=sys.stderr)
    print(json.dumps(payload))
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
    from trustflow.evaluation.errors import safe_error_detail, sanitize_payload

    out = Path(args.out)
    attempt = uuid.uuid4().hex[:12]
    staging = out.with_name(f"{out.name}.staging-{os.getpid()}-{attempt}")
    reports: list = []
    safety_total = 0
    try:
        traces: dict = {}
        for _ in range(repeat):
            report, traces = runner_module.run_evaluation_sync()
            reports.append(report)
            safety_total += report.summary.safety_violation_count
        safe_reports = [report_module.sanitized_report(r) for r in reports]
        digests = [r.verdict_digest() for r in safe_reports]
        reproducible = len(set(digests)) == 1
        if safety_total:
            code = 2
        elif any(r.summary.errors for r in reports):
            code = 3
        elif not reproducible:
            code = 4
        elif any(r.summary.failed for r in reports):
            code = 1
        else:
            code = 0
        # A non-pass attempt never replaces a completed prior
        # publication; it is published separately and identified.
        target = out
        if code != 0 and (out / "COMPLETE.json").exists():
            target = out.with_name(f"{out.name}.failed-{attempt}")
        paths = report_module.write_reports(reports[-1], traces, staging)
        repeats_payload = {
            "repeats": [
                {"index": i, "total": r.summary.total,
                 "passed": r.summary.passed, "failed": r.summary.failed,
                 "errors": r.summary.errors,
                 "safety_violations": r.summary.safety_violation_count,
                 "verdict_digest": d}
                for i, (r, d) in enumerate(zip(reports, digests, strict=True))],
            "reproducible": reproducible,
        }
        repeats_path = staging / "repeats.json"
        report_module._atomic_write(
            repeats_path, json.dumps(sanitize_payload(repeats_payload), indent=2))
        paths["repeats"] = str(repeats_path)
        if args.ablations:
            ablation = ablation_module.run_ablations_sync()
            ablation_path = staging / "ablations.json"
            report_module._atomic_write(
                ablation_path,
                json.dumps(sanitize_payload(ablation), indent=2, default=str))
            paths["ablations"] = str(ablation_path)
        for staged in staging.glob("*.json"):
            json.loads(staged.read_text(encoding="utf-8"))
        marker = sanitize_payload({
            "complete": True,
            "gate_exit_code": code,
            "attempt": attempt,
            "git_sha": reports[-1].run_metadata.git_sha,
            "verdict_digests": digests,
            "reproducible": reproducible,
            "safety_violations_total": safety_total,
            "config_digest": reports[-1].run_metadata.config_digest,
            "dataset_digest": reports[-1].run_metadata.dataset_digest,
        })
        report_module._atomic_write(
            staging / "COMPLETE.json", json.dumps(marker, indent=2))
        # Publish only now that every required artifact succeeded.
        if target.exists():
            backup = out.with_name(f"{out.name}.previous-{attempt}")
            os.rename(target, backup)
            try:
                os.rename(staging, target)
            except Exception:
                os.rename(backup, target)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        else:
            os.rename(staging, target)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        code = 2 if safety_total else 3
        return _fail(safe_error_detail(exc), code, {
            "safety_violations": safety_total,
            "repeats_completed": len(reports),
            "published": False})

    published = {name: str(target / Path(str(path)).name)
                 for name, path in paths.items()}
    print(json.dumps({
        "total": reports[-1].summary.total,
        "passed": reports[-1].summary.passed,
        "failed": reports[-1].summary.failed,
        "errors": reports[-1].summary.errors,
        "safety_violations": safety_total,
        "repeats": repeats_payload["repeats"],
        "verdict_digest": digests[-1],
        "reproducible": reproducible,
        "config_digest": reports[-1].run_metadata.config_digest,
        "published_to": str(target),
        "artifacts": published}, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
