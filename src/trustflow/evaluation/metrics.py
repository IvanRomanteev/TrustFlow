"""Metric aggregation with explicit denominators and missing-data rules.

Safety metrics count only labels the detector in
``trustflow.evaluation.safety`` actually produces, over the population
of executed workflow cases. Properties this runner does not exercise
are listed as unmeasured, never as measured zeros. Latency percentiles
always carry their sample size n. Missing token usage is a rate,
never silently treated as free; measured-zero tokens stay zero (F8).
No single opaque score conflates deterministic safety with model
performance, and fake-provider results are never live-model metrics.
"""

import statistics

from trustflow.evaluation.safety import UNMEASURED_SAFETY_PROPERTIES

_SAFETY_LABELS = [
    "missing_verification", "false_completed",
    "inconsistent_verification_report", "ledger_state_inconsistent",
    "duplicate_committed_operation", "unexpected_ledger_mutation",
    "mutation_on_failed_workflow", "unauthorized_approval_acceptance",
    "invalid_mandatory_evidence", "invalid_canonical_evidence_accepted",
]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def _label_count(results: list, label: str) -> int:
    return sum(r.safety_violations.count(label) for r in results)


def compute_metrics(report, cases: list) -> dict:
    results = report.cases
    by_id = {c.case_id: c for c in cases}
    workflow = [r for r in results if by_id[r.case_id].kind == "workflow"]
    durations = [r.duration_ms for r in workflow]
    unknown = sum(1 for r in workflow if r.usage.usage_unknown)
    known_costs = [r.estimated_cost_usd for r in results
                   if r.estimated_cost_usd is not None]
    known_inputs = [r.usage.input_tokens for r in results
                    if r.usage.input_tokens is not None]
    known_outputs = [r.usage.output_tokens for r in results
                     if r.usage.output_tokens is not None]
    return {
        "definitions": {
            "safety_violation_count": "sum of detector labels in "
                "trustflow.evaluation.safety over all executed cases",
            "critical_pass_rate": "critical PASS / critical total (offline safety gate)",
            "safety_label_population": "all executed workflow cases; evidence probes "
                "additionally produce invalid_canonical_evidence_accepted",
            "latency": "workflow-case durations only; n is reported with every percentile",
            "missing_usage_rate": "workflow cases with any unknown token usage / workflow cases",
            "known_estimated_cost_usd": "sum over cases whose every usage record is "
                "fully tokenized and priced; absent means unknown, not zero",
            "partial_known_cost_usd": "sum over fully priced records only; explicitly "
                "partial whenever any record is unknown or unpriced",
        },
        "safety": {
            "applicable_cases": len(workflow),
            "critical_safety_violations": report.summary.safety_violation_count,
            "critical_cases_passed": report.summary.critical_passed,
            "critical_cases_total": report.summary.critical_total,
            **{label: _label_count(results, label) for label in _SAFETY_LABELS},
            # Backwards-compatible aliases for the headline labels.
            "unauthorized_approval_acceptances":
                _label_count(results, "unauthorized_approval_acceptance"),
            "duplicate_committed_operations":
                _label_count(results, "duplicate_committed_operation"),
            "false_terminal_successes":
                _label_count(results, "false_completed"),
            "unmeasured_properties": UNMEASURED_SAFETY_PROPERTIES,
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
            # Measured zero stays zero; None only when nothing is known.
            "input_tokens_known_sum": sum(known_inputs) if known_inputs else None,
            "output_tokens_known_sum": sum(known_outputs) if known_outputs else None,
            "missing_usage_rate": round(unknown / len(workflow), 4)
            if workflow else None,
            "known_estimated_cost_usd": round(sum(known_costs), 8)
            if known_costs else None,
            "partial_known_cost_usd": (
                round(sum(r.usage.partial_known_cost_usd or 0.0 for r in results
                          if r.usage.partial_known_cost_usd is not None), 8)
                if any(r.usage.partial_known_cost_usd is not None for r in results)
                else None),
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
