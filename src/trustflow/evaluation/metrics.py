"""Metric aggregation with explicit denominators and missing-data rules.

Every metric names its applicable population. Latency percentiles are
reported only with their sample size n; p95 over a handful of cases
is descriptive, not a statistically robust tail estimate. Missing
token usage is reported as a rate, never silently treated as free.
No single opaque score conflates deterministic safety with model
performance, and fake-provider results are never live-model metrics.
"""

import statistics


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def compute_metrics(report, cases: list) -> dict:
    results = report.cases
    by_id = {c.case_id: c for c in cases}
    workflow = [r for r in results if by_id[r.case_id].kind == "workflow"]
    durations = [r.duration_ms for r in workflow]
    usage_cases = [r for r in workflow if r.usage.transport_attempts or True]
    unknown = sum(1 for r in usage_cases if r.usage.usage_unknown)
    known_costs = [r.estimated_cost_usd for r in results
                   if r.estimated_cost_usd is not None]
    return {
        "definitions": {
            "safety_violation_count": "sum of per-case safety_violations over all cases",
            "critical_pass_rate": "critical PASS / critical total (offline safety gate)",
            "latency": "workflow-case durations only; n is reported with every percentile",
            "missing_usage_rate": "workflow cases with any unknown token usage / workflow cases",
            "known_estimated_cost_usd": "sum over cases with complete known tokens and a "
                                        "configured price; absent means unknown, not zero",
        },
        "safety": {
            "critical_safety_violations": report.summary.safety_violation_count,
            "critical_cases_passed": report.summary.critical_passed,
            "critical_cases_total": report.summary.critical_total,
            "unauthorized_approval_acceptances": sum(
                1 for r in results if "unauthorized_approval" in r.safety_violations),
            "duplicate_committed_operations": sum(
                1 for r in results if "unexpected_ledger_mutation" in r.safety_violations),
            "false_terminal_successes": sum(
                1 for r in results if "false_completed" in r.safety_violations),
        },
        "outcomes": {
            "total": report.summary.total, "passed": report.summary.passed,
            "failed": report.summary.failed, "errors": report.summary.errors,
            "expected_policy_rejections": report.summary.expected_policy_rejections,
            "completed_workflows": report.summary.completed_workflows,
            "blocked_or_failed_workflows": report.summary.blocked_or_failed_workflows,
            "label": "offline workflow safety regression pass rate (fake provider); "
                     "not a general LLM accuracy measurement",
        },
        "performance": {
            "n": len(durations),
            "median_ms": round(statistics.median(durations), 2) if durations else None,
            "p95_ms": _pct(durations, 0.95),
            "note": "p95 is descriptive at this n; not a robust tail-latency estimate",
        },
        "usage": {
            "logical_calls": sum(r.usage.logical_calls for r in results),
            "transport_attempts": sum(r.usage.transport_attempts for r in results),
            "input_tokens_known_sum": sum(
                r.usage.input_tokens or 0 for r in results
                if r.usage.input_tokens is not None) or None,
            "missing_usage_rate": round(unknown / len(usage_cases), 4)
            if usage_cases else None,
            "known_estimated_cost_usd": round(sum(known_costs), 8) if known_costs else None,
            "unpriced_or_unknown_cases": sum(
                1 for r in results if r.estimated_cost_usd is None),
        },
        "evidence": {
            "evidence_cases": sum(1 for c in cases if c.category == "evidence"),
            "evidence_cases_passed": sum(
                1 for r in results if r.category == "evidence" and r.status == "PASS"),
            "workflow_cases_with_citations": sum(
                1 for r in workflow if r.evidence_refs),
        },
    }
