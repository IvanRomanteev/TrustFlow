"""Backend-owned semantic context; execution evidence never decides goal correctness."""

import inspect
from typing import Any

from trustflow.domain.models import VerificationReport, WorkflowRun


def review_context_kwargs(
    reviewer: Any, run: WorkflowRun, report: VerificationReport, verification_only: bool
) -> dict[str, Any]:
    """Opt in by actual callable signature; no TypeError retry or shared state.

    Legacy reviewers remain callable. Values are copied from the persisted run
    and the current deterministic report, never supplied by the model.
    """
    params = inspect.signature(reviewer.review).parameters
    if "execution_context" not in params and not any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        return {}
    snapshot = run.approved_plan_snapshot
    return {
        "execution_context": {
            "run_id": run.run_id,
            "approved_hash": run.approved_hash or None,
            "approved_snapshot_hash": snapshot.plan_hash if snapshot is not None else None,
            "human_approval_matches": any(
                a.decision.value == "APPROVED"
                and a.plan_hash == run.approved_hash
                and snapshot is not None
                and a.plan_version == snapshot.version
                for a in run.approvals
            ),
            "verification_only_retry": verification_only,
            "verification_attempt": run.verification_attempts,
            "intents": [
                {
                    "intent_id": i.intent_id,
                    "authorized": i.authorized,
                    "status": i.status.value,
                    "authorized_payload": dict(i.authorized_payload),
                }
                for i in run.intents
            ],
            "deterministic_verification": {
                "source": "Verifier.verify",
                "scope": "execution, ledger and intent checks; not goal correctness",
                "passed": report.passed,
                "checks_total": len(report.checks),
                "checks_passed": sum(c.passed for c in report.checks),
                "failed_checks": [c.name for c in report.checks if not c.passed],
            },
        }
    }
