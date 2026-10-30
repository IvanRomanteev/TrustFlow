"""Typed workflow state. All financial data is synthetic."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


def _now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class RunStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    # A worker may still commit; no definitive failure has been declared.
    EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
    VERIFYING = "VERIFYING"
    # Deterministic report persisted; semantic review pending/failed.
    # Recovery is verification-only and never re-executes tools.
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    NEEDS_REVISION = "NEEDS_REVISION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    # Timed out while a worker may still commit. Not a definitive failure.
    UNCERTAIN = "UNCERTAIN"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# Validated transitions: current -> allowed next states.
TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.PLANNING, RunStatus.CANCELLED}),
    RunStatus.PLANNING: frozenset({RunStatus.PLANNED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.PLANNED: frozenset(
        {RunStatus.AWAITING_APPROVAL, RunStatus.EXECUTING, RunStatus.CANCELLED}
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {RunStatus.EXECUTING, RunStatus.REJECTED, RunStatus.CANCELLED}
    ),
    RunStatus.EXECUTING: frozenset(
        {RunStatus.VERIFYING, RunStatus.EXECUTION_UNCERTAIN,
         RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.EXECUTION_UNCERTAIN: frozenset(
        {RunStatus.VERIFYING, RunStatus.EXECUTING, RunStatus.FAILED,
         RunStatus.CANCELLED}
    ),
    RunStatus.VERIFYING: frozenset(
        {RunStatus.COMPLETED, RunStatus.NEEDS_REVISION, RunStatus.FAILED,
         RunStatus.VERIFICATION_PENDING}
    ),
    RunStatus.VERIFICATION_PENDING: frozenset(
        {RunStatus.VERIFYING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.NEEDS_REVISION: frozenset(
        {RunStatus.PLANNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.REJECTED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.REJECTED, RunStatus.CANCELLED}
)


class WorkflowGoal(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    requested_by: str = Field(default="demo-user", max_length=100)
    # M2 explicit opt-in only: mandatory evidence citations for this
    # workflow (docs/m2.md). Default False keeps every M1 workflow
    # advisory; retrieval never becomes mandatory implicitly.
    require_evidence: bool = False


class PlanStep(BaseModel):
    step_id: str
    tool_name: str
    # Stable business-intent identity, backend-owned, independent of
    # revision order or step index. operation_id mirrors it for the ledger.
    intent_id: str = ""
    operation_id: str = ""
    args: dict = Field(default_factory=dict)
    # LLM-suggested risk only; PolicyEngine overwrites with deterministic risk.
    suggested_risk: RiskLevel = RiskLevel.LOW
    risk: RiskLevel = RiskLevel.LOW
    rationale: str = ""


class Plan(BaseModel):
    plan_id: str = Field(default_factory=new_id)
    goal_title: str
    version: int = 1
    steps: list[PlanStep] = Field(default_factory=list)

    def canonical(self) -> str:
        payload = {
            "goal_title": self.goal_title,
            "version": self.version,
            "steps": [
                {
                    "step_id": s.step_id,
                    "tool_name": s.tool_name,
                    "intent_id": s.intent_id,
                    "operation_id": s.operation_id,
                    "args": s.args,
                }
                for s in self.steps
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def plan_hash(self) -> str:
        """Immutable binding for approvals: hash of version + steps."""
        return hashlib.sha256(self.canonical().encode()).hexdigest()


class StepResult(BaseModel):
    step_id: str
    tool_name: str
    status: StepStatus
    data: dict = Field(default_factory=dict)
    error: str | None = None
    idempotency_key: str = ""


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class SemanticReview(BaseModel):
    verdict: str = "PASS"
    confidence: float = 0.0
    issues: list[str] = Field(default_factory=list)
    summary: str = ""
    model: str = ""


class VerificationReport(BaseModel):
    passed: bool
    checks: list[VerificationCheck] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    # Advisory only: a semantic PASS can never override a deterministic FAIL.
    semantic: SemanticReview | None = None


class ApprovalRecord(BaseModel):
    plan_version: int
    plan_hash: str
    decision: ApprovalDecision
    actor: str
    reason: str = ""
    created_at: datetime = Field(default_factory=_now)


class IntentStatus(StrEnum):
    PROPOSED = "PROPOSED"      # new/modified, not yet authorized
    AUTHORIZED = "AUTHORIZED"  # authorized payload is executable
    COMMITTED = "COMMITTED"    # exactly one ledger tx under this intent
    CONFLICT = "CONFLICT"      # committed intent was changed: fail closed
    CANCELLED = "CANCELLED"    # explicitly cancelled while uncommitted


class IntentRecord(BaseModel):
    """Authoritative payment intent. Identity is independent of payload,
    revision order and step position; payloads are never silently rewritten."""

    intent_id: str
    tool_name: str = "simulate_transfer"
    authorized_payload: dict = Field(default_factory=dict)
    proposed_payload: dict = Field(default_factory=dict)
    fingerprint: str = ""  # fingerprint of the authorized payload
    status: IntentStatus = IntentStatus.PROPOSED
    authorized: bool = False
    ever_authorized: bool = False  # cancellation of such intents needs evidence


class WorkflowRun(BaseModel):
    run_id: str = Field(default_factory=new_id)
    goal: WorkflowGoal
    status: RunStatus = RunStatus.CREATED
    plan: Plan | None = None
    step_results: list[StepResult] = Field(default_factory=list)
    verification: VerificationReport | None = None
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    attempt: int = 0
    verification_attempts: int = 0
    # Backend-owned operation registry: fingerprint+ordinal -> operation_id.
    operations: list[dict] = Field(default_factory=list)
    # Authoritative stable payment intents (round-2 manifest).
    intents: list[IntentRecord] = Field(default_factory=list)
    # Reserved for a future authorized-cancellation workflow. M1 has no
    # such workflow and creates no records here; the verifier rejects any
    # nonempty content as unsupported evidence (never as authorization).
    # Trusted-state boundary: runs live in single-process memory, so only
    # trusted in-process code can mutate this or replace IntentRecords;
    # no public API accepts intents, cancellations or run state.
    cancellations: list[dict] = Field(default_factory=list)
    # Isolated deep snapshot taken at approval time; execution uses this.
    approved_plan_snapshot: Plan | None = None
    approved_hash: str = ""
    llm_calls: list[dict] = Field(default_factory=list)
    # M2 advisory retrieval evidence (docs/m2.md): retrieved policy
    # chunks, evidence spans (policy + ledger) and the ledger snapshot
    # the ledger spans cite. Advisory unless goal.require_evidence.
    retrieval: list[dict] = Field(default_factory=list)
    evidence_spans: list[dict] = Field(default_factory=list)
    ledger_snapshot: dict | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL
