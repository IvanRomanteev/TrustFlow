"""WorkflowEngine: Planner -> Approval -> Executor -> Verifier (+semantic).

Verification recovery is verification-only: retrying semantic review never
executes tools, creates transfers, or triggers mutation-producing re-plans.
"""

import hashlib
import json
import uuid

from trustflow.agents.approval import HumanApprovalService
from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.audit.logger import AuditLogger
from trustflow.core.errors import (
    ApprovalRequiredError,
    ConflictingOperationError,
    TrustFlowError,
)
from trustflow.core.logging import sanitize_error
from trustflow.core.settings import Settings
from trustflow.domain.models import (
    Plan,
    RunStatus,
    StepStatus,
    WorkflowGoal,
    WorkflowRun,
)
from trustflow.domain.state_machine import transition
from trustflow.llm.usage import UsageTracker
from trustflow.policies.policy_engine import PolicyEngine


class RunRepository:
    """In-memory run store. Swap for SQLAlchemy later behind this API."""

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}

    def save(self, run: WorkflowRun) -> WorkflowRun:
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> WorkflowRun | None:
        return self._runs.get(run_id)

    def list(self) -> list[WorkflowRun]:
        return list(self._runs.values())


def payload_fingerprint(step) -> str:
    """Canonical fingerprint of the executable payload (tool + normalized args)."""
    canonical = json.dumps(
        {"tool": step.tool_name, "args": step.args}, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def assign_operation_ids(run: WorkflowRun, plan: Plan) -> Plan:
    """Backend-owned payment-intent identities.

    Identity is a fresh UUID per authorized intent, retained across retries
    and revisions by matching (payload fingerprint, occurrence ordinal) in
    the run registry. It is never derived from route/amount/version/position
    alone, and never assigned by the LLM. Reuse with a conflicting payload
    registered under the same identity fails explicitly.
    """
    ordinals: dict[str, int] = {}
    by_id = {op["operation_id"]: op for op in run.operations}
    for step in plan.steps:
        if step.tool_name != "simulate_transfer":
            continue
        fingerprint = payload_fingerprint(step)
        ordinal = ordinals.get(fingerprint, 0)
        ordinals[fingerprint] = ordinal + 1
        if step.operation_id and step.operation_id in by_id:
            registered = by_id[step.operation_id]
            if registered["fingerprint"] != fingerprint:
                raise ConflictingOperationError(
                    f"Operation {step.operation_id} reused with conflicting payload"
                )
            continue
        match = next(
            (op for op in run.operations
             if op["fingerprint"] == fingerprint and op["ordinal"] == ordinal),
            None,
        )
        if match is not None:
            step.operation_id = match["operation_id"]
        else:
            step.operation_id = f"op_{uuid.uuid4().hex[:16]}"
            run.operations.append({
                "operation_id": step.operation_id,
                "fingerprint": fingerprint,
                "ordinal": ordinal,
                "tool_name": step.tool_name,
            })
    return plan


class WorkflowEngine:
    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        verifier: Verifier,
        policy: PolicyEngine | None = None,
        approvals: HumanApprovalService | None = None,
        audit: AuditLogger | None = None,
        repo: RunRepository | None = None,
        settings: Settings | None = None,
        semantic: SemanticReviewer | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.policy = policy or PolicyEngine(settings)
        self.approvals = approvals or HumanApprovalService()
        self.audit = audit or AuditLogger()
        self.repo = repo or RunRepository()
        self.settings = settings or Settings()
        self.semantic = semantic
        self._trackers: dict[str, UsageTracker] = {}

    def _tracker(self, run: WorkflowRun) -> UsageTracker:
        if run.run_id not in self._trackers:
            self._trackers[run.run_id] = UsageTracker(
                self.settings.token_budget_per_workflow, workflow_id=run.run_id
            )
        return self._trackers[run.run_id]

    def _persist_usage(self, run: WorkflowRun) -> None:
        run.llm_calls = [r.model_dump() for r in self._tracker(run).records]

    def _execution_plan(self, run: WorkflowRun) -> Plan:
        source = run.approved_plan_snapshot or run.plan
        assert source is not None
        return source.model_copy(deep=True)

    async def create_workflow(self, goal: WorkflowGoal) -> WorkflowRun:
        run = WorkflowRun(goal=goal)
        self.repo.save(run)
        self.audit.log(run.run_id, goal.requested_by, "workflow_created", {"title": goal.title})
        transition(run, RunStatus.PLANNING)
        try:
            plan = assign_operation_ids(
                run, self.planner.create_plan(goal, version=1, tracker=self._tracker(run))
            )
        except TrustFlowError as exc:
            run.error = sanitize_error(str(exc))
            self._persist_usage(run)  # failed planning keeps its incurred usage
            transition(run, RunStatus.FAILED)
            return self.repo.save(run)
        self._persist_usage(run)
        run.plan = plan
        self.audit.log(run.run_id, "planner", "plan_created",
                       {"version": plan.version, "plan_hash": plan.plan_hash,
                        "steps": len(plan.steps)})
        bound_issues = self.policy.check_bounds(plan)
        transition(run, RunStatus.PLANNED)
        if bound_issues:
            run.error = "; ".join(bound_issues)
            transition(run, RunStatus.EXECUTING)
            transition(run, RunStatus.VERIFYING)
            transition(run, RunStatus.FAILED)
            return self.repo.save(run)
        if self.policy.requires_approval(plan):
            transition(run, RunStatus.AWAITING_APPROVAL)
            self.audit.log(run.run_id, "policy", "approval_required",
                           {"plan_hash": plan.plan_hash})
            return self.repo.save(run)
        transition(run, RunStatus.EXECUTING)
        return await self._execute_and_verify(run)

    async def resume_after_approval(self, run: WorkflowRun) -> WorkflowRun:
        if run.status != RunStatus.EXECUTING or not self.approvals.is_approved(run):
            raise ApprovalRequiredError("Workflow has no valid approval for its current plan")
        self.audit.log(run.run_id, "system", "execution_resumed", {})
        return await self._execute_and_verify(run)

    async def reconcile(self, run: WorkflowRun) -> WorkflowRun:
        """Resolve uncertain outcomes without any new tool execution."""
        if run.status != RunStatus.EXECUTION_UNCERTAIN:
            return run
        plan = self._execution_plan(run)
        run.step_results = await self.executor.reconcile(run, plan)
        if any(r.status == StepStatus.UNCERTAIN for r in run.step_results):
            return self.repo.save(run)  # still unresolved; replacements blocked
        transition(run, RunStatus.VERIFYING)
        return await self._verify_loop(run, plan)

    async def retry_verification(self, run: WorkflowRun) -> WorkflowRun:
        """Verification-only recovery: no tools, no transfers, no re-planning."""
        if run.status != RunStatus.VERIFICATION_PENDING:
            return run
        transition(run, RunStatus.VERIFYING)
        plan = self._execution_plan(run)
        return await self._verify_loop(run, plan, semantic_only_retry=True)

    async def _execute_and_verify(self, run: WorkflowRun) -> WorkflowRun:
        plan = self._execution_plan(run)
        run.step_results = await self.executor.execute(run, plan)
        self.audit.log(run.run_id, "executor", "execution_finished",
                       {"results": len(run.step_results)})
        if any(r.status == StepStatus.UNCERTAIN for r in run.step_results):
            transition(run, RunStatus.EXECUTION_UNCERTAIN)
            run.error = "Execution outcome uncertain; reconcile before any replacement"
            return self.repo.save(run)
        transition(run, RunStatus.VERIFYING)
        return await self._verify_loop(run, plan)

    async def _verify_loop(self, run: WorkflowRun, plan: Plan,
                           semantic_only_retry: bool = False) -> WorkflowRun:
        while True:
            report = self.verifier.verify(run, plan)
            # Deterministic report + committed evidence persist BEFORE any
            # semantic call, so a reviewer failure cannot strand the run.
            run.verification = report
            self.repo.save(run)
            if report.passed and self.semantic is not None:
                try:
                    review = self.semantic.review(run.goal, plan,
                                                  tracker=self._tracker(run))
                    report.semantic = review
                    run.verification = report
                    if review.verdict != "PASS":
                        report.passed = False
                        report.issues.append(
                            "Semantic review FAIL: " + "; ".join(review.issues)
                        )
                except TrustFlowError as exc:
                    self._persist_usage(run)
                    run.error = sanitize_error(
                        f"Semantic review unavailable: {exc}. "
                        "Committed transfers (if any) were not rolled back."
                    )
                    transition(run, RunStatus.VERIFICATION_PENDING)
                    return self.repo.save(run)
                self._persist_usage(run)
                run.verification = report
            self.audit.log(run.run_id, "verifier", "verification_finished",
                           {"passed": report.passed, "issues": report.issues})
            if report.passed:
                transition(run, RunStatus.COMPLETED)
                return self.repo.save(run)
            # Semantic-origin failures recover verification-only, bounded.
            semantic_failed = bool(
                report.semantic is not None and report.semantic.verdict != "PASS"
            ) or semantic_only_retry
            if semantic_failed:
                run.verification_attempts += 1
                if run.verification_attempts > self.settings.max_retries:
                    run.error = (
                        "Semantic verification failed after bounded retries. "
                        "Committed transfers (if any) were not rolled back: "
                        + "; ".join(report.issues)
                    )
                    transition(run, RunStatus.FAILED)
                    return self.repo.save(run)
                transition(run, RunStatus.VERIFICATION_PENDING)
                return self.repo.save(run)
            if run.attempt >= self.settings.max_retries:
                run.error = "Retry budget exhausted: " + "; ".join(report.issues)
                transition(run, RunStatus.FAILED)
                return self.repo.save(run)
            run.attempt += 1
            transition(run, RunStatus.NEEDS_REVISION)
            transition(run, RunStatus.PLANNING)
            try:
                run.plan = assign_operation_ids(
                    run,
                    self.planner.create_plan(
                        run.goal, version=plan.version + 1,
                        tracker=self._tracker(run),
                    ),
                )
                self._persist_usage(run)
            except TrustFlowError as exc:
                run.error = sanitize_error(str(exc))
                self._persist_usage(run)
                transition(run, RunStatus.FAILED)
                return self.repo.save(run)
            run.approved_plan_snapshot = None
            self.audit.log(run.run_id, "planner", "plan_revised",
                           {"version": run.plan.version, "attempt": run.attempt})
            transition(run, RunStatus.PLANNED)
            if self.policy.requires_approval(run.plan):
                transition(run, RunStatus.AWAITING_APPROVAL)
                return self.repo.save(run)
            transition(run, RunStatus.EXECUTING)
            plan = self._execution_plan(run)
            run.step_results = await self.executor.execute(run, plan)
            if any(r.status == StepStatus.UNCERTAIN for r in run.step_results):
                transition(run, RunStatus.EXECUTION_UNCERTAIN)
                return self.repo.save(run)
            transition(run, RunStatus.VERIFYING)
