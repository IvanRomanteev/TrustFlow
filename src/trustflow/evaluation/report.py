"""Report writers: JSON, CSV, Markdown and per-run trace exports.

Writes are atomic (temporary file + replace) and failures raise:
a report that cannot be written must never look like a success.
Generated artifacts contain no credentials: case results carry only
ids, outcomes, counts and redacted trace data.
"""

import csv
import io
import json
import os
from pathlib import Path

from trustflow.evaluation.schemas import EvalReport
from trustflow.evaluation.trace import render_timeline

_CSV_FIELDS = ["case_id", "category", "status", "expected_outcome",
               "observed_outcome", "duration_ms", "tx_count",
               "failure_category", "safety_violations", "logical_calls",
               "transport_attempts", "input_tokens", "output_tokens",
               "estimated_cost_usd", "workflow_run_id"]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def render_csv(report: EvalReport) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    for case in report.cases:
        writer.writerow({
            "case_id": case.case_id, "category": case.category,
            "status": case.status, "expected_outcome": case.expected_outcome,
            "observed_outcome": case.observed_outcome,
            "duration_ms": case.duration_ms, "tx_count": case.tx_count,
            "failure_category": case.failure_category,
            "safety_violations": ";".join(case.safety_violations),
            "logical_calls": case.usage.logical_calls,
            "transport_attempts": case.usage.transport_attempts,
            "input_tokens": case.usage.input_tokens,
            "output_tokens": case.usage.output_tokens,
            "estimated_cost_usd": case.estimated_cost_usd,
            "workflow_run_id": case.workflow_run_id})
    return out.getvalue()


def render_markdown(report: EvalReport) -> str:
    meta, summary = report.run_metadata, report.summary
    lines = [
        "# TrustFlow Offline Evaluation Report (M4)",
        "",
        "SANDBOX / NO REAL FUNDS. Fake provider only; these are offline "
        "workflow safety regression results, not live-model performance.",
        "",
        f"- Git SHA: `{meta.git_sha}` (dirty: {meta.git_dirty})",
        f"- Dataset: `{meta.dataset_version}` digest `{meta.dataset_digest[:16]}…`",
        f"- Seed: {meta.seed}; provider: {meta.provider_mode}; "
        f"python: {meta.python_version}",
        "",
        "## Summary",
        "",
        f"- Cases: {summary.passed}/{summary.total} PASS "
        f"(failed {summary.failed}, errors {summary.errors})",
        f"- Critical safety cases: {summary.critical_passed}/{summary.critical_total}",
        f"- Critical safety violations: {summary.safety_violation_count}",
        f"- Expected policy rejections (successful safety outcomes): "
        f"{summary.expected_policy_rejections}",
        "",
        "## Cases",
        "",
        "| Case | Category | Status | Expected | Observed | tx | ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in report.cases:
        lines.append(
            f"| {case.case_id} | {case.category} | {case.status} | "
            f"{case.expected_outcome} | {case.observed_outcome} | "
            f"{case.tx_count} | {case.duration_ms} |")
    failures = [c for c in report.cases if c.status != "PASS"]
    if failures:
        lines += ["", "## Failures", ""]
        for case in failures:
            lines.append(f"- **{case.case_id}** ({case.failure_category}): "
                         f"{case.detail or case.observed_outcome}; "
                         f"violations: {case.safety_violations}")
    metrics = report.metrics or {}
    lines += ["", "## Metrics", "", "```json",
              json.dumps(metrics, indent=2, default=str), "```", ""]
    return "\n".join(lines)


def write_reports(report: EvalReport, traces: dict, out_dir: str | Path) -> dict[str, str]:
    """Write all artifacts; returns artifact name -> path. Raises on failure."""
    out = Path(out_dir)
    paths: dict[str, str] = {}
    _atomic_write(out / "evaluation-results.json",
                  json.dumps(report.model_dump(), indent=2, default=str))
    paths["json"] = str(out / "evaluation-results.json")
    _atomic_write(out / "evaluation-summary.csv", render_csv(report))
    paths["csv"] = str(out / "evaluation-summary.csv")
    _atomic_write(out / "evaluation-report.md", render_markdown(report))
    paths["markdown"] = str(out / "evaluation-report.md")
    _atomic_write(out / "traces.json", json.dumps(traces, indent=2, default=str))
    paths["traces"] = str(out / "traces.json")
    for run_id, trace in traces.items():
        name = f"trace-{run_id}.md"
        _atomic_write(out / name, render_timeline(trace))
        paths[f"timeline:{run_id}"] = str(out / name)
    _atomic_write(out / "reproducibility-manifest.json", json.dumps({
        "git_sha": report.run_metadata.git_sha,
        "git_dirty": report.run_metadata.git_dirty,
        "dataset_version": report.run_metadata.dataset_version,
        "dataset_digest": report.run_metadata.dataset_digest,
        "seed": report.run_metadata.seed,
        "provider_mode": report.run_metadata.provider_mode,
        "python_version": report.run_metadata.python_version,
        "verdict_digest": report.verdict_digest(),
        "note": "verdict_digest excludes timestamps, run ids and durations",
    }, indent=2))
    paths["manifest"] = str(out / "reproducibility-manifest.json")
    return paths
