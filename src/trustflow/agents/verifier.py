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

    # Legal intent lifecycle transitions. CANCELLED and CONFLICT are
    # terminal for sync purposes and can never become COMMITTED silently.
    _SYNC_ALLOWED = frozenset({"AUTHORIZED"})

    def sync_intents(self, run: WorkflowRun) -> None:
        """Reflect ledger commits into intent statuses (evidence, not claims)."""
        if self._ledger is None:
            return
        from trustflow.domain.models import IntentStatus

        for intent in run.intents:
            if intent.status.value not in self._SYNC_ALLOWED:
                continue
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
                # Authoritative identity comes from the canonical run-owned
                # intent record, never from tool results or plan claims.
                key = step.operation_id
                intent = next(
                    (i for i in run.intents
                     if i.intent_id == (step.intent_id or step.operation_id)),
                    None,
                )
                lifecycle_ok = bool(
                    intent is not None
                    and intent.status.value in ("AUTHORIZED", "COMMITTED")
                )
                tx = self._ledger.find_by_key(key) if self._ledger is not None else None
                ok = bool(
                    lifecycle_ok
                    and tx is not None
                    and tx.idempotency_key == (intent.intent_id if intent else "")
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

            # M1 has no authorized-cancellation workflow, so no record in
            # run.cancellations can be valid evidence in M1: any nonempty
            # content is unsupported and fails verification, whatever its
            # intent_id, actor, decision, fingerprint or timestamp claims.
            if run.cancellations:
                checks.append(VerificationCheck(
                    name="unsupported_cancellation_evidence", passed=False,
                    detail=f"records={len(run.cancellations)}"))
                issues.append(
                    "Unsupported cancellation evidence: M1 has no authorized "
                    "cancellation workflow; run.cancellations records cannot "
                    "satisfy or erase authorized obligations"
                )
            for intent in run.intents:
                if intent.status in (IntentStatus.CANCELLED, IntentStatus.PROPOSED):
                    # PROPOSED/CANCELLED can never launder ledger evidence:
                    # a tx under such an intent is an integrity conflict.
                    # A CANCELLED ever-authorized intent never satisfies its
                    # obligation in M1, regardless of cancellation records.
                    stray = [t for t in self._ledger.transactions
                             if t.idempotency_key == intent.intent_id]
                    if stray:
                        checks.append(VerificationCheck(
                            name=f"intent_integrity:{intent.intent_id}", passed=False,
                            detail="Ledger tx exists for non-executable intent"))
                        issues.append(
                            f"Intent {intent.intent_id} has ledger evidence in "
                            f"lifecycle {intent.status}; integrity conflict"
                        )
                    if intent.status == IntentStatus.CANCELLED \
                            and intent.ever_authorized:
                        checks.append(VerificationCheck(
                            name=f"intent_cancellation:{intent.intent_id}",
                            passed=False,
                            detail="ever-authorized intent is CANCELLED; "
                                   "cancellation is unsupported in M1"))
                        issues.append(
                            f"Intent {intent.intent_id} is CANCELLED after "
                            "authorization; M1 has no authorized cancellation "
                            "workflow, so the authorized obligation remains "
                            "unresolved"
                        )
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
