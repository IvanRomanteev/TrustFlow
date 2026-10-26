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

    def sync_intents(self, run: WorkflowRun) -> None:
        """Reflect ledger commits into intent statuses (evidence, not claims)."""
        if self._ledger is None:
            return
        from trustflow.domain.models import IntentStatus

        for intent in run.intents:
            if self._ledger.find_by_key(intent.intent_id) is not None:
                intent.status = IntentStatus.COMMITTED

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
                # Authoritative identity comes from the approved execution
                # snapshot (the plan passed in), never from tool results.
                key = step.operation_id
                tx = self._ledger.find_by_key(key) if self._ledger is not None else None
                ok = bool(
                    tx is not None
                    and res is not None
                    and res.idempotency_key == key
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

        # Manifest coverage: every required authorized intent must have
        # exactly one matching committed ledger operation before COMPLETED.
        if self._ledger is not None:
            from trustflow.domain.models import IntentStatus

            for intent in run.intents:
                if intent.status in (IntentStatus.CANCELLED, IntentStatus.PROPOSED):
                    continue
                if intent.status == IntentStatus.CONFLICT:
                    checks.append(VerificationCheck(
                        name=f"intent_conflict:{intent.intent_id}", passed=False))
                    issues.append(f"Intent {intent.intent_id} is in conflict")
                    continue
                txs = [t for t in self._ledger.transactions
                       if t.idempotency_key == intent.intent_id]
                payload = intent.authorized_payload
                ok = bool(
                    len(txs) == 1
                    and txs[0].from_account == payload.get("from_account")
                    and txs[0].to_account == payload.get("to_account")
                    and float(txs[0].amount) == float(payload.get("amount", -1))
                    and txs[0].currency == payload.get("currency", "USD")
                )
                checks.append(VerificationCheck(
                    name=f"intent_coverage:{intent.intent_id}", passed=ok,
                    detail=f"committed={len(txs)}"))
                if not ok:
                    issues.append(
                        f"Intent {intent.intent_id} lacks exactly one matching "
                        "committed operation"
                    )

        return VerificationReport(passed=all(c.passed for c in checks), checks=checks,
                                  issues=issues)
