"""HumanApprovalService: approvals bound to immutable plan hash + version."""

from trustflow.core.errors import ApprovalError
from trustflow.domain.models import ApprovalDecision, ApprovalRecord, RunStatus, WorkflowRun
from trustflow.domain.state_machine import transition


class HumanApprovalService:
    """Only explicit, hash-matched, single-use approvals unlock execution."""

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
        return transition(run, RunStatus.EXECUTING)

    def reject(self, run: WorkflowRun, actor: str, plan_hash: str, reason: str = "") -> WorkflowRun:
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
        if run.plan is None:
            return False
        return any(
            r.decision == ApprovalDecision.APPROVED
            and r.plan_hash == run.plan.plan_hash
            and r.plan_version == run.plan.version
            for r in run.approvals
        )
