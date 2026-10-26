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

        # Transfers are verified against the ledger itself, never against
        # tx ids or success fields claimed only by tool results.
        for step in plan.steps:
            if step.tool_name == "simulate_transfer":
                res = by_id.get(step.step_id)
                key = (res.idempotency_key if res else "") or step.operation_id
                tx = self._ledger.find_by_key(key) if self._ledger is not None else None
                ok = bool(
                    tx is not None
                    and res is not None
                    and res.status == StepStatus.SUCCEEDED
                    and tx.from_account == step.args.get("from_account")
                    and tx.to_account == step.args.get("to_account")
                    and float(tx.amount) == float(step.args.get("amount", -1))
                    and tx.currency == step.args.get("currency", "USD")
                    and res.data.get("tx_id") == tx.tx_id
                )
                checks.append(VerificationCheck(
                    name=f"transfer_ledger_match:{step.step_id}", passed=ok,
                    detail=f"key={key} ledger_tx={tx.tx_id if tx else None}"))
                if not ok:
                    issues.append(
                        f"Transfer {step.step_id} does not match an independently "
                        "read ledger transaction for its approved operation"
                    )

        return VerificationReport(passed=all(c.passed for c in checks), checks=checks,
                                  issues=issues)
