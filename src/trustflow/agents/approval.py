"""HumanApprovalService: approvals bound to immutable plan hash + version."""

import threading

from trustflow.core.errors import ApprovalError
from trustflow.domain.models import ApprovalDecision, ApprovalRecord, RunStatus, WorkflowRun
from trustflow.domain.state_machine import transition


class HumanApprovalService:
    """Only explicit, hash-matched, single-use approvals unlock execution.

    A lock makes validate+record+transition atomic in-process, so concurrent
    or repeated approval requests cannot both succeed (replay prevention).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _validate(self, run: WorkflowRun, plan_hash: str) -> None:
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ApprovalError(f"Run {run.run_id} is not awaiting approval ({run.status})")
        if run.plan is None:
            raise ApprovalError("Run has no plan to approve")
        if plan_hash != run.plan.plan_hash:
            raise ApprovalError("Plan hash mismatch: approval is bound to another plan version")
        for record in run.approvals:
            if record.plan_hash == plan_hash and record.plan_version == run.plan.version:
                raise ApprovalError("This plan version was already decided (repeated approval)")

    def approve(
        self, run: WorkflowRun, actor: str, plan_hash: str, reason: str = ""
    ) -> WorkflowRun:
        with self._lock:
            return self._approve_locked(run, actor, plan_hash, reason)

    def _approve_locked(
        self, run: WorkflowRun, actor: str, plan_hash: str, reason: str = ""
    ) -> WorkflowRun:
        self._validate(run, plan_hash)
        assert run.plan is not None
        run.approvals.append(
            ApprovalRecord(
                plan_version=run.plan.version,
                plan_hash=plan_hash,
                decision=ApprovalDecision.APPROVED,
                actor=actor,
                reason=reason,
            )
        )
        # Isolated snapshot: later mutation of run.plan cannot alter what
        # was approved (deep copy covers nested args dicts).
        run.approved_plan_snapshot = run.plan.model_copy(deep=True)
        run.approved_hash = plan_hash
        # Approving authorizes exactly the snapshot's payment intents, with
        # the snapshot payloads as their canonical authorized payloads.
        from trustflow.domain.models import IntentStatus
        from trustflow.orchestration.engine import fingerprint_of

        for step in run.approved_plan_snapshot.steps:
            if step.tool_name != "simulate_transfer":
                continue
            for intent in run.intents:
                if intent.intent_id == (step.intent_id or step.operation_id):
                    intent.authorized_payload = step.model_copy(deep=True).args
                    intent.proposed_payload = step.model_copy(deep=True).args
                    intent.fingerprint = fingerprint_of(step.tool_name, step.args)
                    intent.status = IntentStatus.AUTHORIZED
                    intent.authorized = True
                    intent.ever_authorized = True
        return transition(run, RunStatus.EXECUTING)

    def reject(self, run: WorkflowRun, actor: str, plan_hash: str, reason: str = "") -> WorkflowRun:
        with self._lock:
            return self._reject_locked(run, actor, plan_hash, reason)

    def _reject_locked(
        self, run: WorkflowRun, actor: str, plan_hash: str, reason: str = ""
    ) -> WorkflowRun:
        self._validate(run, plan_hash)
        assert run.plan is not None
        run.approvals.append(
            ApprovalRecord(
                plan_version=run.plan.version,
                plan_hash=plan_hash,
                decision=ApprovalDecision.REJECTED,
                actor=actor,
                reason=reason,
            )
        )
        run.error = reason or "Rejected by human approver"
        return transition(run, RunStatus.REJECTED)

    def is_approved(self, run: WorkflowRun) -> bool:
        """Only the exact approved snapshot counts; run.plan alone never does."""
        snapshot = run.approved_plan_snapshot
        if snapshot is None or not run.approved_hash:
            return False
        return any(
            r.decision == ApprovalDecision.APPROVED
            and r.plan_hash == run.approved_hash == snapshot.plan_hash
            and r.plan_version == snapshot.version
            for r in run.approvals
        )
