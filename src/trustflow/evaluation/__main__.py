"""One-command offline evaluation: python -m trustflow.evaluation

Lifecycle: validate inputs -> execute all repeats, preserving a
sanitized per-repeat evidence record as each repeat completes ->
execute ablations -> compute the gate outcome -> sanitize and write
every artifact into a private staging directory -> validate staged
JSON -> publish by rename -> COMPLETE.json (part of the staged set)
marks a fully published attempt; it is not by itself an evaluation
PASS.

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
owner. If a later stage fails after one or more repeats completed,
the completed-repeat evidence is retained in a separately identified
``<name>.failed-<id>/FAILED_ATTEMPT.json`` artifact when writing is
still possible, and always in the bounded sanitized stdout fallback;
no COMPLETE marker is created for an incomplete attempt. Errors are
printed sanitized and bounded; tracebacks are never published (F4).
Fake provider only: no network or paid API calls.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

EXIT_USAGE = 64

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{7,64}\Z")
_MAX_EVIDENCE_REPEATS = 50
_MAX_EVIDENCE_CASES = 100
_MAX_EVIDENCE_TRACES = 100
_MAX_TRACE_BYTES = 200_000


def _fail(message: str, code: int, extra: dict | None = None) -> int:
    from trustflow.evaluation.errors import sanitize_payload

    payload = sanitize_payload({"error": message, **(extra or {})})
    print(json.dumps(payload), file=sys.stderr)
    print(json.dumps(payload))
    return code


def _validate_provenance(report) -> None:
    """Reject malformed critical provenance instead of publishing it
    as a genuine reproducibility claim. Values are never echoed."""
    meta = report.run_metadata
    for name in ("config_digest", "dataset_digest",
                 "assertion_contract_digest"):
        value = getattr(meta, name)
        if value and not _HEX64.fullmatch(value):
            raise ValueError(f"invalid {name} in run provenance")
    if meta.git_sha and not _GIT_SHA.fullmatch(meta.git_sha):
        raise ValueError("invalid git_sha in run provenance")


def _repeat_record(index, report, verdict_digest, traces, attempt):
    """Bounded sanitized evidence for one completed repeat."""
    from trustflow.evaluation.errors import sanitize_payload

    findings = [
        {"case_id": c.case_id, "status": c.status,
         "safety_violations": list(c.safety_violations),
         "failure_category": c.failure_category,
         "workflow_run_id": c.workflow_run_id,
         "trace_ref": c.trace_ref}
        for c in report.cases[:_MAX_EVIDENCE_CASES]
        if c.safety_violations or c.status != "PASS"
    ]
    trace_ids = sorted(str(k) for k in list(traces)[:_MAX_EVIDENCE_TRACES])
    trace_digest = hashlib.sha256(
        json.dumps(traces, sort_keys=True, default=str).encode()
    ).hexdigest() if traces else ""
    return sanitize_payload({
        "index": index,
        "attempt": attempt,
        "git_sha": report.run_metadata.git_sha,
        "config_digest": report.run_metadata.config_digest,
        "dataset_digest": report.run_metadata.dataset_digest,
        "verdict_digest": verdict_digest,
        "total": report.summary.total,
        "passed": report.summary.passed,
        "failed": report.summary.failed,
        "errors": report.summary.errors,
        "safety_violations": report.summary.safety_violation_count,
        "findings": findings,
        "trace_ids": trace_ids,
        "trace_digest": trace_digest,
    })


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
    safe_reports: list = []
    digests: list[str] = []
    completed_records: list[dict] = []
    evidence_traces: dict = {}
    evidence_overflow = 0
    safety_total = 0
    traces: dict = {}
    try:
        for index in range(repeat):
            report, traces = runner_module.run_evaluation_sync()
            reports.append(report)
            safety_total += report.summary.safety_violation_count
            safe = report_module.sanitized_report(report)
            safe_reports.append(safe)
            digests.append(safe.verdict_digest())
            safe_traces = sanitize_payload(traces)
            if not isinstance(safe_traces, dict):
                safe_traces = {}
            record = _repeat_record(index, safe, digests[-1],
                                    safe_traces, attempt)
            if len(completed_records) < _MAX_EVIDENCE_REPEATS:
                completed_records.append(record)
            else:
                evidence_overflow += 1
            for trace_id, trace in list(safe_traces.items())[
                    :_MAX_EVIDENCE_TRACES]:
                if len(evidence_traces) < _MAX_EVIDENCE_TRACES:
                    evidence_traces[str(trace_id)] = trace
            _validate_provenance(safe)
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
        paths = report_module.write_reports(safe_reports[-1], traces, staging)
        repeats_payload = sanitize_payload({
            "repeats": [
                {"index": i, "total": r.summary.total,
                 "passed": r.summary.passed, "failed": r.summary.failed,
                 "errors": r.summary.errors,
                 "safety_violations": r.summary.safety_violation_count,
                 "verdict_digest": d}
                for i, (r, d) in enumerate(
                    zip(safe_reports, digests, strict=True))],
            "reproducible": reproducible,
        })
        repeats_path = staging / "repeats.json"
        report_module._atomic_write(
            repeats_path, json.dumps(repeats_payload, indent=2))
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
            "git_sha": safe_reports[-1].run_metadata.git_sha,
            "verdict_digests": digests,
            "reproducible": reproducible,
            "safety_violations_total": safety_total,
            "config_digest": safe_reports[-1].run_metadata.config_digest,
            "dataset_digest": safe_reports[-1].run_metadata.dataset_digest,
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
        git_sha = (completed_records[-1].get("git_sha", "")
                   if completed_records else "")
        failure = sanitize_payload({
            "error": safe_error_detail(exc),
            "harness_error_category": type(exc).__name__,
            "error_category": type(exc).__name__,
            "attempt": attempt,
            "candidate_sha": git_sha,
            "git_sha": git_sha,
            "safety_violations": safety_total,
            "repeats_completed": len(reports),
            "completed_repeats": completed_records,
            "evidence_overflow_repeats": evidence_overflow,
            "published": False,
            "complete": False,
            "publication_complete": False,
            "publication_status": "incomplete",
        })
        try:
            failed_target = out.with_name(f"{out.name}.failed-{attempt}")
            report_module._atomic_write(
                failed_target / "FAILED_ATTEMPT.json",
                json.dumps(failure, indent=2))
            if evidence_traces:
                traces_text = json.dumps(
                    sanitize_payload(evidence_traces), indent=2, default=str)
                if len(traces_text.encode()) > _MAX_TRACE_BYTES:
                    traces_text = json.dumps(sanitize_payload({
                        "truncated": True,
                        "reason": "trace evidence size bound",
                        "trace_ids": sorted(evidence_traces),
                        "completed_repeats": completed_records,
                    }), indent=2)
                report_module._atomic_write(
                    failed_target / "failed-traces.json", traces_text)
            failure = {**failure,
                       "failure_artifact": str(
                           failed_target / "FAILED_ATTEMPT.json")}
        except Exception:
            failure = {**failure, "failure_artifact": None}
        return _fail(str(failure.get("error", "harness error")), code,
                     {k: v for k, v in failure.items() if k != "error"})

    published = {name: str(target / Path(str(path)).name)
                 for name, path in paths.items()}
    # The finalized sanitized report is the only publication source,
    # including for stdout: no raw metadata is read after finalization.
    final = sanitize_payload({
        "total": safe_reports[-1].summary.total,
        "passed": safe_reports[-1].summary.passed,
        "failed": safe_reports[-1].summary.failed,
        "errors": safe_reports[-1].summary.errors,
        "safety_violations": safety_total,
        "repeats": repeats_payload["repeats"],
        "verdict_digest": digests[-1],
        "reproducible": reproducible,
        "config_digest": safe_reports[-1].run_metadata.config_digest,
        "dataset_digest": safe_reports[-1].run_metadata.dataset_digest,
        "git_sha": safe_reports[-1].run_metadata.git_sha,
        "attempt": attempt,
        "published_to": str(target),
        "artifacts": published})
    print(json.dumps(final, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
