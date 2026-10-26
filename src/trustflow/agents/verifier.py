"""Verifier: independent checks over execution results (self-verification)."""

from trustflow.domain.models import (
    Plan,
    StepStatus,
    VerificationCheck,
    VerificationReport,
    WorkflowRun,
)
from trustflow.tools.ledger import SyntheticLedger


class Verifier:
    """Uses muse-wonder-class reasoning in production; deterministic in M0."""

    def __init__(self, ledger: SyntheticLedger | None = None, expected_total: float | None = None):
        self._ledger = ledger
        self._expected_total = expected_total

    def verify(self, run: WorkflowRun, plan: Plan) -> VerificationReport:
        checks: list[VerificationCheck] = []
        issues: list[str] = []
        by_id = {r.step_id: r for r in run.step_results}

        all_done = all(
            by_id.get(s.step_id) is not None
            and by_id[s.step_id].status == StepStatus.SUCCEEDED
            for s in plan.steps
        )
        checks.append(
            VerificationCheck(name="all_steps_succeeded", passed=all_done,
                              detail=f"{len(run.step_results)}/{len(plan.steps)} results")
        )
        if not all_done:
            issues.append("Not all plan steps succeeded")

        failed = [r.step_id for r in run.step_results if r.status == StepStatus.FAILED]
        checks.append(VerificationCheck(name="no_failed_steps", passed=not failed,
                                        detail=str(failed)))
        if failed:
            issues.append(f"Failed steps: {failed}")

        if self._ledger is not None and self._expected_total is not None:
            conserved = abs(self._ledger.total_balance() - self._expected_total) < 1e-9
            checks.append(VerificationCheck(name="ledger_conservation", passed=conserved,
                                            detail=f"total={self._ledger.total_balance()}"))
            if not conserved:
                issues.append("Synthetic ledger total changed unexpectedly")

        # A transfer step must carry an idempotency key and a tx id.
        for step in plan.steps:
            if step.tool_name == "simulate_transfer":
                res = by_id.get(step.step_id)
                ok = bool(res and res.idempotency_key and res.data.get("tx_id"))
                checks.append(VerificationCheck(name=f"transfer_idempotent:{step.step_id}",
                                                passed=ok))
                if not ok:
                    issues.append(f"Transfer {step.step_id} missing idempotent tx evidence")

        return VerificationReport(passed=all(c.passed for c in checks), checks=checks,
                                  issues=issues)
