"""WorkflowEngine: Planner -> Approval -> Executor -> Verifier (+semantic)."""

from trustflow.agents.approval import HumanApprovalService
from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.audit.logger import AuditLogger
from trustflow.core.errors import ApprovalRequiredError, TrustFlowError
from trustflow.core.settings import Settings
from trustflow.domain.models import Plan, RunStatus, WorkflowGoal, WorkflowRun
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


def assign_operation_ids(run: WorkflowRun, plan: Plan) -> Plan:
    """Stable operation identity: same logical transfer keeps one identity
    across retries, resume and re-planning (never a new key for a new plan)."""
    for step in plan.steps:
        if step.tool_name == "simulate_transfer" and not step.operation_id:
            step.operation_id = (
                f"{run.run_id}:simulate_transfer:"
                f"{step.args.get('from_account')}->{step.args.get('to_account')}"
                f":{step.args.get('currency', 'USD')}"
            )
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
                self.settings.token_budget_per_workflow
            )
        return self._trackers[run.run_id]

    def _sync_usage(self, run: WorkflowRun) -> None:
        tracker = self._tracker(run)
        for source in (self.planner.calls, self.semantic.calls if self.semantic else []):
            while source:
                tracker.record(source.pop(0))
        run.llm_calls = [r.model_dump() for r in tracker.records]

    async def create_workflow(self, goal: WorkflowGoal) -> WorkflowRun:
        run = WorkflowRun(goal=goal)
        self.repo.save(run)
        self.audit.log(run.run_id, goal.requested_by, "workflow_created", {"title": goal.title})
        transition(run, RunStatus.PLANNING)
        try:
            plan = assign_operation_ids(run, self.planner.create_plan(goal, version=1))
            self._sync_usage(run)
        except TrustFlowError as exc:
            run.error = str(exc)[:500]
            transition(run, RunStatus.FAILED)
            return self.repo.save(run)
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

    async def _execute_and_verify(self, run: WorkflowRun) -> WorkflowRun:
        assert run.plan is not None
        while True:
            run.step_results = await self.executor.execute(run, run.plan)
            self.audit.log(run.run_id, "executor", "execution_finished",
                           {"results": len(run.step_results)})
            if run.status == RunStatus.EXECUTING:
                transition(run, RunStatus.VERIFYING)
            report = self.verifier.verify(run, run.plan)
            # Semantic review can only add failures; it never rescues a
            # deterministic FAIL. Deterministic result stays authoritative.
            if report.passed and self.semantic is not None:
                review = self.semantic.review(run.goal, run.plan)
                report.semantic = review
                try:
                    self._sync_usage(run)
                except TrustFlowError as exc:
                    report.passed = False
                    report.issues.append(str(exc))
                if review.verdict != "PASS":
                    report.passed = False
                    report.issues.append(
                        "Semantic review FAIL: " + "; ".join(review.issues)
                    )
            run.verification = report
            self.audit.log(run.run_id, "verifier", "verification_finished",
                           {"passed": report.passed, "issues": report.issues})
            if report.passed:
                transition(run, RunStatus.COMPLETED)
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
                    run, self.planner.create_plan(run.goal, version=run.plan.version + 1)
                )
                self._sync_usage(run)
            except TrustFlowError as exc:
                run.error = str(exc)[:500]
                transition(run, RunStatus.FAILED)
                return self.repo.save(run)
            self.audit.log(run.run_id, "planner", "plan_revised",
                           {"version": run.plan.version, "attempt": run.attempt})
            transition(run, RunStatus.PLANNED)
            if self.policy.requires_approval(run.plan):
                transition(run, RunStatus.AWAITING_APPROVAL)
                return self.repo.save(run)
            transition(run, RunStatus.EXECUTING)
