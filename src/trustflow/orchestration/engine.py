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


def fingerprint_of(tool_name: str, args: dict) -> str:
    canonical = json.dumps(
        {"tool": tool_name, "args": args}, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _intent_match_fp(intent) -> str:
    payload = intent.authorized_payload if intent.authorized else intent.proposed_payload
    return fingerprint_of(intent.tool_name, payload)


def assign_operation_ids(run: WorkflowRun, plan: Plan,
                         new_intents_authorized: bool = True) -> Plan:
    """Stable business-intent assignment (round 2).

    Identity is independent of revision order/step index and is never taken
    from the LLM. Matching for ID-less revisions: exact authorized/proposed
    payload match when unique (identical payloads match distinct intents by
    count). A differing step against a committed intent is an explicit
    conflict; a single differing step against a single uncommitted intent is
    a modification requiring re-authorization; anything ambiguous fails
    closed. Revision arguments are never overwritten with manifest payloads.
    """
    from trustflow.core.errors import IntentConflictError
    from trustflow.domain.models import IntentRecord, IntentStatus

    matched: set[str] = set()
    mutating = [s for s in plan.steps if s.tool_name == "simulate_transfer"]

    def assign(step, intent) -> None:
        step.intent_id = intent.intent_id
        step.operation_id = intent.intent_id
        matched.add(intent.intent_id)

    remaining = []
    for step in mutating:  # pass 1: exact payload matches (order-independent)
        fp = payload_fingerprint(step)
        candidate = next(
            (i for i in run.intents
             if i.intent_id not in matched
             and i.status != IntentStatus.CANCELLED
             and _intent_match_fp(i) == fp),
            None,
        )
        if candidate is not None:
            assign(step, candidate)
        else:
            remaining.append(step)

    unmatched = [i for i in run.intents
                 if i.intent_id not in matched and i.status != IntentStatus.CANCELLED]

    def route(payload: dict) -> tuple:
        return (payload.get("from_account"), payload.get("to_account"))

    def intent_route(intent) -> tuple:
        payload = intent.authorized_payload or intent.proposed_payload
        return route(payload)

    # Route-grouped matching: a differing step can only modify an intent on
    # the same route; a committed intent on that route makes it a conflict.
    new_steps = []
    consumed: set[str] = set()
    steps_by_route: dict[tuple, list] = {}
    for step in remaining:
        steps_by_route.setdefault(route(step.args), []).append(step)
    intents_by_route: dict[tuple, list] = {}
    for intent in unmatched:
        intents_by_route.setdefault(intent_route(intent), []).append(intent)
    for group_route, group_steps in steps_by_route.items():
        group_intents = intents_by_route.get(group_route, [])
        if not group_intents:
            new_steps.extend(group_steps)
            continue
        if any(i.status == IntentStatus.COMMITTED for i in group_intents):
            for intent in group_intents:
                if intent.status == IntentStatus.COMMITTED:
                    intent.status = IntentStatus.CONFLICT
            raise IntentConflictError(
                "intent_conflict: a committed payment intent was changed by a revision"
            )
        if len(group_steps) == 1 and len(group_intents) == 1:
            intent = group_intents[0]
            intent.proposed_payload = json.loads(json.dumps(group_steps[0].args))
            intent.status = IntentStatus.PROPOSED
            intent.authorized = False
            assign(group_steps[0], intent)
            consumed.add(intent.intent_id)
        else:
            raise IntentConflictError(
                "intent_conflict: ambiguous ID-less revision cannot be matched safely"
            )
    # Single-intent legacy case: one differing step, one uncommitted intent
    # overall (route may have changed as part of the modification).
    if new_steps and len(new_steps) == len(remaining) and len(unmatched) == 1 \
            and not consumed:
        intent = unmatched[0]
        if intent.status == IntentStatus.COMMITTED and len(remaining) == 1:
            intent.status = IntentStatus.CONFLICT
            raise IntentConflictError(
                "intent_conflict: a committed payment intent was changed by a revision"
            )
        if intent.status != IntentStatus.COMMITTED and len(remaining) == 1:
            intent.proposed_payload = json.loads(json.dumps(remaining[0].args))
            intent.status = IntentStatus.PROPOSED
            intent.authorized = False
            assign(remaining[0], intent)
            consumed.add(intent.intent_id)
            new_steps = []
    for step in new_steps:  # genuinely new business intents
        intent = IntentRecord(
            intent_id=f"op_{uuid.uuid4().hex[:16]}",
            tool_name=step.tool_name,
            proposed_payload=json.loads(json.dumps(step.args)),
            authorized_payload=(
                json.loads(json.dumps(step.args)) if new_intents_authorized else {}
            ),
            fingerprint=payload_fingerprint(step) if new_intents_authorized else "",
            status=IntentStatus.AUTHORIZED if new_intents_authorized
            else IntentStatus.PROPOSED,
            authorized=bool(new_intents_authorized),
            ever_authorized=bool(new_intents_authorized),
        )
        run.intents.append(intent)
        assign(step, intent)
    # Omission policy (never infer cancellation consent from an LLM revision):
    # COMMITTED is preserved; AUTHORIZED remains an outstanding obligation
    # and is an explicit conflict; only never-authorized PROPOSED intents
    # may be withdrawn by omission.
    for intent in run.intents:
        if intent.intent_id in matched or intent.intent_id in consumed:
            continue
        if intent.status == IntentStatus.COMMITTED:
            continue
        if intent.status == IntentStatus.PROPOSED and not intent.ever_authorized:
            intent.status = IntentStatus.CANCELLED
            intent.authorized = False
            continue
        if intent.status in (IntentStatus.CANCELLED, IntentStatus.CONFLICT):
            continue
        raise IntentConflictError(
            "intent_conflict: an authorized intent was omitted from a revision; "
            "cancellation requires explicit authorized cancellation evidence"
        )
    # Legacy mirror for older readers of run.operations.
    run.operations = [
        {"operation_id": i.intent_id, "fingerprint": i.fingerprint,
         "tool_name": i.tool_name, "payload": i.authorized_payload or i.proposed_payload,
         "authorized": i.authorized}
        for i in run.intents
    ]
    return plan


def has_unauthorized_intents(run: WorkflowRun, plan: Plan) -> bool:
    by_id = {i.intent_id: i for i in run.intents}
    for step in plan.steps:
        if step.tool_name != "simulate_transfer":
            continue
        intent = by_id.get(step.intent_id or step.operation_id)
        if intent is None or not intent.authorized:
            return True
    return False


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
        plan = source.model_copy(deep=True)
        # Executable arguments are the intent's canonical authorized payload.
        by_id = {i.intent_id: i for i in run.intents}
        for step in plan.steps:
            intent = by_id.get(step.intent_id or step.operation_id)
            if intent is not None and intent.authorized:
                step.args = json.loads(json.dumps(intent.authorized_payload))
        return plan

    async def create_workflow(self, goal: WorkflowGoal) -> WorkflowRun:
        run = WorkflowRun(goal=goal)
        self.repo.save(run)
        self.audit.log(run.run_id, goal.requested_by, "workflow_created", {"title": goal.title})
        transition(run, RunStatus.PLANNING)
        try:
            planned = self.planner.create_plan(goal, version=1,
                                               tracker=self._tracker(run))
            plan = assign_operation_ids(
                run, planned,
                new_intents_authorized=not self.policy.requires_approval(planned),
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
            self.verifier.sync_intents(run)
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
                    exhausted = (
                        run.verification_attempts
                        >= self.settings.semantic_max_attempts
                    )
                    # History survives recovery: the outage is audited
                    # (sanitized) before this branch returns, so clearing
                    # the active error on later success erases nothing.
                    self.audit.log(
                        run.run_id, "verifier", "semantic_review_failed",
                        {"error": run.error,
                         "attempt": run.verification_attempts,
                         "recovery_state": (
                             RunStatus.FAILED if exhausted
                             else RunStatus.VERIFICATION_PENDING
                         )},
                    )
                    if exhausted:
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
                # Bounds apply to every revision, before its intents are
                # assigned/authorized and before any step executes; a new
                # approval never lifts the deterministic limits.
                bound_issues = self.policy.check_bounds(revised)
                if bound_issues:
                    run.error = "; ".join(bound_issues)
                    self._persist_usage(run)
                    transition(run, RunStatus.FAILED)
                    return self.repo.save(run)
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
