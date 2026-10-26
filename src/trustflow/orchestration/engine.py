"""WorkflowEngine: Planner -> Approval -> Executor -> Verifier (+semantic).

Verification recovery is verification-only: retrying semantic review never
executes tools, creates transfers, or triggers mutation-producing re-plans.
"""

import hashlib
import json
import threading
import uuid

from trustflow.agents.approval import HumanApprovalService
from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.audit.logger import AuditLogger
from trustflow.core.errors import ApprovalRequiredError, TrustFlowError
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


def assign_operation_ids(run: WorkflowRun, plan: Plan,
                         new_intents_authorized: bool = True) -> Plan:
    """Authoritative backend-owned payment-intent manifest.

    The manifest (run.operations, in intent order) is the source of truth,
    separate from payload matching. The Nth mutating intent of any revision
    maps to the Nth manifest intent: its identity AND its originally
    authorized payload are preserved, so a re-plan proposing 101 for a
    committed 100 can never create a second payment. Mutating intents beyond
    the manifest are new business operations: they are registered as
    unauthorized and require explicit approval before execution.
    """
    manifest = [op for op in run.operations if op.get("tool_name") == "simulate_transfer"]
    position = 0
    for step in plan.steps:
        if step.tool_name != "simulate_transfer":
            continue
        if position < len(manifest):
            entry = manifest[position]
            step.operation_id = entry["operation_id"]
            # Preserve the authorized intent payload exactly (deep copy).
            step.args = json.loads(json.dumps(entry["payload"]))
        else:
            step.operation_id = f"op_{uuid.uuid4().hex[:16]}"
            entry = {
                "operation_id": step.operation_id,
                "fingerprint": payload_fingerprint(step),
                "ordinal": position,
                "tool_name": step.tool_name,
                "payload": json.loads(json.dumps(step.args)),
                "authorized": bool(new_intents_authorized),
            }
            run.operations.append(entry)
            manifest.append(entry)
        position += 1
    return plan


def has_unauthorized_intents(run: WorkflowRun, plan: Plan) -> bool:
    by_id = {op["operation_id"]: op for op in run.operations}
    return any(
        step.tool_name == "simulate_transfer"
        and not by_id.get(step.operation_id, {}).get("authorized", False)
        for step in plan.steps
    )


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
        self._claim_lock = threading.Lock()
        self._inflight: set[str] = set()

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
        # Exact-snapshot binding: only the approved snapshot may execute,
        # and its digest must match the approval record exactly.
        snapshot = run.approved_plan_snapshot
        if (
            run.status != RunStatus.EXECUTING
            or snapshot is None
            or not run.approved_hash
            or snapshot.plan_hash != run.approved_hash
            or not self.approvals.is_approved(run)
        ):
            raise ApprovalRequiredError(
                "Workflow has no intact approved snapshot for execution"
            )
        self.audit.log(run.run_id, "system", "execution_resumed", {})
        return await self._execute_and_verify(run)

    async def reconcile(self, run: WorkflowRun) -> WorkflowRun:
        """Resolve uncertain outcomes. Concurrent callers get the current
        state back (defined response), never a transition exception."""
        with self._claim_lock:
            if run.status != RunStatus.EXECUTION_UNCERTAIN or run.run_id in self._inflight:
                return run
            self._inflight.add(run.run_id)
        try:
            plan = self._execution_plan(run)
            run.step_results = await self.executor.reconcile(run, plan)
            if any(r.status == StepStatus.UNCERTAIN for r in run.step_results):
                return self.repo.save(run)
            transition(run, RunStatus.VERIFYING)
            return await self._verify_loop(run, plan)
        finally:
            with self._claim_lock:
                self._inflight.discard(run.run_id)

    async def retry_verification(self, run: WorkflowRun) -> WorkflowRun:
        """Verification-only recovery: no tools, no transfers, no re-planning.
        Concurrent retries are claimed atomically; others get current state."""
        with self._claim_lock:
            if run.status != RunStatus.VERIFICATION_PENDING or run.run_id in self._inflight:
                return run
            self._inflight.add(run.run_id)
        try:
            transition(run, RunStatus.VERIFYING)
            plan = self._execution_plan(run)
            return await self._verify_loop(run, plan, semantic_only_retry=True)
        finally:
            with self._claim_lock:
                self._inflight.discard(run.run_id)

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
            semantic_failed = False
            if report.passed and self.semantic is not None:
                # Every attempt counts (initial call included), checked and
                # incremented atomically before the provider is called.
                with self._claim_lock:
                    exhausted = (
                        run.verification_attempts >= self.settings.semantic_max_attempts
                    )
                    if not exhausted:
                        run.verification_attempts += 1
                if exhausted:
                    run.error = (
                        "Semantic attempt limit reached. Committed transfers "
                        "(if any) were not rolled back."
                    )
                    transition(run, RunStatus.FAILED)
                    return self.repo.save(run)
                try:
                    review = self.semantic.review(run.goal, plan,
                                                  tracker=self._tracker(run))
                    report.semantic = review
                    run.verification = report
                    if review.verdict != "PASS":
                        semantic_failed = True
                        report.passed = False
                        report.issues.append(
                            "Semantic review FAIL: "
                            + sanitize_error("; ".join(review.issues))
                        )
                except TrustFlowError as exc:
                    self._persist_usage(run)
                    run.error = sanitize_error(
                        f"Semantic review unavailable: {exc}. "
                        "Committed transfers (if any) were not rolled back."
                    )
                    if run.verification_attempts >= self.settings.semantic_max_attempts:
                        transition(run, RunStatus.FAILED)
                    else:
                        transition(run, RunStatus.VERIFICATION_PENDING)
                    return self.repo.save(run)
                self._persist_usage(run)
                run.verification = report
            self.audit.log(run.run_id, "verifier", "verification_finished",
                           {"passed": report.passed, "issues": report.issues})
            if report.passed:
                run.error = None  # resolved; history stays in the audit trail
                transition(run, RunStatus.COMPLETED)
                return self.repo.save(run)
            if semantic_failed or semantic_only_retry:
                run.error = sanitize_error(
                    "Semantic review FAIL. Committed transfers (if any) were "
                    "not rolled back: " + "; ".join(report.issues)
                )
                if run.verification_attempts >= self.settings.semantic_max_attempts:
                    transition(run, RunStatus.FAILED)
                else:
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
                revised = self.planner.create_plan(
                    run.goal, version=plan.version + 1,
                    tracker=self._tracker(run),
                )
                # New intents in a revision are unauthorized until approved.
                run.plan = assign_operation_ids(
                    run, revised, new_intents_authorized=False
                )
                self.policy.apply(run.plan)
                self._persist_usage(run)
            except TrustFlowError as exc:
                run.error = sanitize_error(str(exc))
                self._persist_usage(run)
                transition(run, RunStatus.FAILED)
                return self.repo.save(run)
            run.approved_plan_snapshot = None
            run.approved_hash = ""
            self.audit.log(run.run_id, "planner", "plan_revised",
                           {"version": run.plan.version, "attempt": run.attempt})
            transition(run, RunStatus.PLANNED)
            if self.policy.requires_approval(run.plan) or has_unauthorized_intents(
                run, run.plan
            ):
                transition(run, RunStatus.AWAITING_APPROVAL)
                return self.repo.save(run)
            transition(run, RunStatus.EXECUTING)
            plan = self._execution_plan(run)
            run.step_results = await self.executor.execute(run, plan)
            if any(r.status == StepStatus.UNCERTAIN for r in run.step_results):
                transition(run, RunStatus.EXECUTION_UNCERTAIN)
                return self.repo.save(run)
            transition(run, RunStatus.VERIFYING)
