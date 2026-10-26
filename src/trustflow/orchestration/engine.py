"""WorkflowEngine: drives Planner -> Approval -> Executor -> Verifier."""

from trustflow.agents.approval import HumanApprovalService
from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.verifier import Verifier
from trustflow.audit.logger import AuditLogger
from trustflow.core.errors import ApprovalRequiredError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal, WorkflowRun
from trustflow.domain.state_machine import transition
from trustflow.policies.policy_engine import PolicyEngine


class RunRepository:
    """In-memory run store (M0). Swap for SQLAlchemy in M1 behind this API."""

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}

    def save(self, run: WorkflowRun) -> WorkflowRun:
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> WorkflowRun | None:
        return self._runs.get(run_id)

    def list(self) -> list[WorkflowRun]:
        return list(self._runs.values())


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
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.policy = policy or PolicyEngine(settings)
        self.approvals = approvals or HumanApprovalService()
        self.audit = audit or AuditLogger()
        self.repo = repo or RunRepository()
        self.settings = settings or Settings()

    async def create_workflow(self, goal: WorkflowGoal) -> WorkflowRun:
        """Create and advance a workflow; may return AWAITING_APPROVAL (non-terminal)."""
        run = WorkflowRun(goal=goal)
        self.repo.save(run)
        self.audit.log(run.run_id, goal.requested_by, "workflow_created", {"title": goal.title})
        transition(run, RunStatus.PLANNING)
        plan = self.planner.create_plan(goal, version=1)
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
        """Resume only from EXECUTING state entered via HumanApprovalService."""
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
            # Bounded recovery: replan as a new immutable version.
            run.attempt += 1
            transition(run, RunStatus.NEEDS_REVISION)
            transition(run, RunStatus.PLANNING)
            run.plan = self.planner.create_plan(run.goal, version=run.plan.version + 1)
            self.audit.log(run.run_id, "planner", "plan_revised",
                           {"version": run.plan.version, "attempt": run.attempt})
            transition(run, RunStatus.PLANNED)
            if self.policy.requires_approval(run.plan):
                transition(run, RunStatus.AWAITING_APPROVAL)
                return self.repo.save(run)
            transition(run, RunStatus.EXECUTING)
