"""Behavioral regression probes from the second independent M1 review."""

import asyncio

import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import PLAN_SCHEMA, REVIEW_SCHEMA, FakeLLMProvider
from trustflow.llm.usage import LLMCallRecord
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry

SECRET = "api_key=SYNTH-SECRET-MARKER-9"


def _engine(fake=None, settings=None, semantic=True):
    registry = ToolRegistry()
    s = settings or Settings(max_retries=2)
    policy = PolicyEngine(s)
    provider = fake or FakeLLMProvider()
    engine = WorkflowEngine(
        Planner(provider, policy, max_output_tokens=s.max_output_tokens),
        Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
        semantic=SemanticReviewer(provider, registry.ledger,
                                  max_output_tokens=s.max_output_tokens)
        if semantic else None,
    )
    return engine, registry


def _t(amount, frm="acc_alice", to="acc_bob", ccy="USD"):
    return {"tool_name": "simulate_transfer", "from_account": frm,
            "to_account": to, "amount": amount, "currency": ccy}


class SequenceProvider(FakeLLMProvider):
    """Returns successive scripted plans per propose call."""

    def __init__(self, plans, review=None):
        super().__init__(review=review)
        self._plans = plans
        self._i = 0

    def propose_plan(self, title, description, repair_feedback=None):
        idx = min(self._i, len(self._plans) - 1)
        self._i += 1
        return [dict(s) for s in self._plans[idx]], LLMCallRecord(
            purpose="plan", model="fake")


def _fail_verify_once(engine):
    state = {"done": False}
    orig = engine.verifier.verify

    def flaky(run, plan):
        report = orig(run, plan)
        if not state["done"] and report.passed:
            state["done"] = True
            report.passed = False
            report.issues.append("one-time failure (test)")
        return report

    engine.verifier.verify = flaky  # type: ignore[method-assign]


# P1.1 changed payload on re-plan must not create a second payment
@pytest.mark.parametrize("second", [
    _t(101.0), _t(100.0, to="acc_ops"), _t(100.0, frm="acc_ops", to="acc_bob"),
    _t(100.0, ccy="EUR"),
])
@pytest.mark.asyncio
async def test_replan_changed_payload_keeps_committed_intent(second):
    # Round-2 contract supersedes silent preservation: a changed committed
    # intent is an explicit conflict, never a silent COMPLETED rewrite.
    provider = SequenceProvider([[_t(100.0)], [second]])
    engine, registry = _engine(fake=provider, semantic=False)
    _fail_verify_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay 100"))
    assert run.status != RunStatus.COMPLETED
    assert run.error and "conflict" in run.error.lower()
    amounts = [tx.amount for tx in registry.ledger.transactions]
    assert amounts == [100.0]  # never [100, 101] or a second mutated payment


@pytest.mark.asyncio
async def test_replan_new_identical_step_needs_authorization():
    provider = SequenceProvider([[_t(100.0)], [_t(100.0), _t(100.0)]])
    engine, registry = _engine(fake=provider, semantic=False)
    _fail_verify_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay 100"))
    # The extra intent must not execute silently: it needs explicit approval
    # or the run must not complete with two transfers.
    assert len(registry.ledger.transactions) == 1
    assert run.status in (RunStatus.AWAITING_APPROVAL, RunStatus.COMPLETED)
    if run.status == RunStatus.COMPLETED:
        assert len(registry.ledger.transactions) == 1


@pytest.mark.asyncio
async def test_replan_reordered_and_removed_steps_keep_one_tx():
    provider = SequenceProvider([
        [{"tool_name": "get_balance", "account_id": "acc_alice"}, _t(100.0)],
        [_t(100.0)],  # read removed, transfer reordered to first
    ])
    engine, registry = _engine(fake=provider, semantic=False)
    _fail_verify_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay 100"))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1


# P1.2 approved 1500, snapshot modified to 1700 -> rejected before mutation
@pytest.mark.asyncio
async def test_modified_snapshot_rejected_before_mutation():
    engine, registry = _engine(semantic=False)
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 1500", description="transfer 1500 to bob"))
    assert run.status == RunStatus.AWAITING_APPROVAL
    engine.approvals.approve(run, "mgr", run.plan.plan_hash)
    assert run.approved_plan_snapshot is not None
    for step in run.approved_plan_snapshot.steps:
        if step.tool_name == "simulate_transfer":
            step.args["amount"] = 1700.0
    from trustflow.core.errors import ApprovalRequiredError

    with pytest.raises(ApprovalRequiredError):
        await engine.resume_after_approval(run)
    assert len(registry.ledger.transactions) == 0


# P1.3 unrelated operation evidence must not satisfy current operation
@pytest.mark.asyncio
async def test_unrelated_operation_evidence_rejected():
    engine, registry = _engine(semantic=False)
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob"))
    assert run.status == RunStatus.COMPLETED
    assert run.plan is not None
    for step in run.plan.steps:
        if step.tool_name == "simulate_transfer":
            step.operation_id = "op_unrelated"
    report = engine.verifier.verify(run, run.plan)
    assert report.passed is False


# P2.4 semantic attempts counted including failures; bounded
@pytest.mark.asyncio
async def test_semantic_attempts_bounded_including_exceptions():
    fake = FakeLLMProvider(review={"verdict": "MAYBE", "issues": []})
    engine, _ = _engine(fake=fake, settings=Settings(max_retries=1))
    run = await engine.create_workflow(WorkflowGoal(title="Check", description="balance"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    first = run.verification_attempts
    assert first >= 1  # initial call counts
    for _ in range(5):
        run = await engine.retry_verification(run)
        if run.status == RunStatus.FAILED:
            break
    assert run.status == RunStatus.FAILED
    assert run.verification_attempts <= 3


# P2.5 concurrent reconcile/retry get defined responses, one transaction
@pytest.mark.asyncio
async def test_concurrent_retry_verification_no_crash():
    fake = FakeLLMProvider(review={"verdict": "FAIL", "confidence": 0.5,
                                   "issues": ["x"], "summary": "fail"})
    engine, registry = _engine(fake=fake, settings=Settings(max_retries=5))
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    fake._review = {"verdict": "PASS", "confidence": 0.9, "issues": [], "summary": "ok"}
    results = await asyncio.gather(
        *[engine.retry_verification(run) for _ in range(3)],
        return_exceptions=True,
    )
    assert not any(isinstance(r, Exception) for r in results)
    assert len(registry.ledger.transactions) == 1


# P2.6 provider/local semantic schema alignment
def test_semantic_schema_alignment():
    props = REVIEW_SCHEMA["properties"]
    assert set(REVIEW_SCHEMA["required"]) == {"verdict", "confidence", "issues", "summary"}
    assert props["confidence"].get("minimum") == 0
    assert props["confidence"].get("maximum") == 1
    from trustflow.agents.semantic import SemanticReviewInput

    sample = {"verdict": "PASS", "confidence": 0.5, "issues": ["a"], "summary": "ok"}
    SemanticReviewInput.model_validate(sample)


# P2.8 semantic issues sanitized in workflow-visible state
@pytest.mark.asyncio
async def test_semantic_issues_sanitized_in_run():
    fake = FakeLLMProvider(review={"verdict": "FAIL", "confidence": 0.5,
                                   "issues": [f"leaked {SECRET}"], "summary": "fail"})
    engine, _ = _engine(fake=fake)
    run = await engine.create_workflow(WorkflowGoal(title="Check", description="balance"))
    assert "SYNTH-SECRET" not in str(run.model_dump())
    assert "SYNTH-SECRET" not in str(run.error or "")


# P2.9 full tool-schema parity including limit
def test_full_tool_schema_parity():
    plan_props = set(PLAN_SCHEMA["properties"]["steps"]["items"]["properties"])
    registry = ToolRegistry()
    for name in registry.names():
        fields = set(registry.get(name).args_model.model_fields)
        fields.discard("idempotency_key")
        assert fields <= plan_props, f"{name}: {fields - plan_props} missing from plan schema"
    assert "limit" in plan_props


# P2.10 resolved error cleared after successful recovery
@pytest.mark.asyncio
async def test_error_cleared_after_verification_recovery():
    fake = FakeLLMProvider(review={"verdict": "FAIL", "confidence": 0.5,
                                   "issues": ["temp"], "summary": "fail"})
    engine, _ = _engine(fake=fake)
    run = await engine.create_workflow(WorkflowGoal(title="Check", description="balance"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert run.error
    fake._review = {"verdict": "PASS", "confidence": 0.9, "issues": [], "summary": "ok"}
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert run.error is None
    # History remains in the audit trail.
    actions = [e.action for e in engine.audit.list(run.run_id)]
    assert "verification_finished" in actions
