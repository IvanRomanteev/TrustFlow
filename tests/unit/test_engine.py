import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.verifier import Verifier
from trustflow.core.errors import ApprovalError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, StepStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry


def _engine(scripted=None, settings=None) -> tuple[WorkflowEngine, ToolRegistry]:
    registry = ToolRegistry()
    s = settings or Settings(max_retries=2)
    policy = PolicyEngine(s)
    engine = WorkflowEngine(
        Planner(FakeLLMProvider(scripted), policy),
        Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
    )
    return engine, registry


@pytest.mark.asyncio
async def test_successful_low_risk_execution():
    engine, _ = _engine()
    run = await engine.create_workflow(WorkflowGoal(title="Check balance", description="balance"))
    assert run.status == RunStatus.COMPLETED
    assert run.verification is not None and run.verification.passed


@pytest.mark.asyncio
async def test_high_risk_pauses_for_approval_then_completes():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 5000", description="transfer 5000 to bob")
    )
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.plan is not None
    bob_before = registry.ledger.get_balance("acc_bob").balance
    engine.approvals.approve(run, "alice-manager", run.plan.plan_hash)
    run = await engine.resume_after_approval(run)
    assert run.status == RunStatus.COMPLETED
    assert registry.ledger.get_balance("acc_bob").balance == bob_before + 5000.0


@pytest.mark.asyncio
async def test_rejected_approval_terminates_safely():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 5000", description="transfer 5000 to bob")
    )
    assert run.plan is not None
    bob_before = registry.ledger.get_balance("acc_bob").balance
    engine.approvals.reject(run, "alice-manager", run.plan.plan_hash, "too large")
    assert run.status == RunStatus.REJECTED
    assert run.is_terminal
    assert registry.ledger.get_balance("acc_bob").balance == bob_before


@pytest.mark.asyncio
async def test_repeated_approval_rejected():
    engine, _ = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 5000", description="transfer 5000 to bob")
    )
    assert run.plan is not None
    engine.approvals.approve(run, "mgr", run.plan.plan_hash)
    with pytest.raises(ApprovalError):
        engine.approvals.approve(run, "mgr", run.plan.plan_hash)


@pytest.mark.asyncio
async def test_stale_hash_approval_rejected():
    engine, _ = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 5000", description="transfer 5000 to bob")
    )
    with pytest.raises(ApprovalError):
        engine.approvals.approve(run, "mgr", "0" * 64)


@pytest.mark.asyncio
async def test_duplicate_execution_prevented_on_resume():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 500", description="transfer 500 to bob")
    )
    assert run.status == RunStatus.COMPLETED
    tx_count = len(registry.ledger.transactions)
    # Re-executing the same plan must skip succeeded steps entirely.
    assert run.plan is not None
    again = await engine.executor.execute(run, run.plan)
    assert len(registry.ledger.transactions) == tx_count
    assert all(r.status == StepStatus.SUCCEEDED for r in again)


@pytest.mark.asyncio
async def test_verifier_failure_triggers_revision():
    scripted = [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                 "to_account": "acc_bob", "amount": 999999.0, "currency": "USD"}]
    engine, _ = _engine(scripted=scripted, settings=Settings(max_retries=1))
    run = await engine.create_workflow(WorkflowGoal(title="Huge", description="transfer"))
    # Insufficient funds -> step fails -> verifier fails -> attempts bounded.
    assert run.status in (RunStatus.AWAITING_APPROVAL, RunStatus.FAILED)
    # Each revised plan version needs its own fresh approval.
    for _ in range(4):
        if run.status != RunStatus.AWAITING_APPROVAL:
            break
        assert run.plan is not None
        engine.approvals.approve(run, "mgr", run.plan.plan_hash)
        run = await engine.resume_after_approval(run)
    assert run.status == RunStatus.FAILED
    assert run.attempt >= 1


@pytest.mark.asyncio
async def test_unknown_account_fails_closed_at_planning():
    # M1: deterministic preconditions reject unknown accounts before execution.
    scripted = [{"tool_name": "get_balance", "account_id": "acc_missing"}]
    engine, _ = _engine(scripted=scripted, settings=Settings(max_retries=2))
    run = await engine.create_workflow(WorkflowGoal(title="Bad account", description="check"))
    assert run.status == RunStatus.FAILED
    assert run.error is not None and "account" in run.error.lower()


@pytest.mark.asyncio
async def test_retry_exhaustion_fails_terminally():
    scripted = [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                 "to_account": "acc_bob", "amount": 999999.0, "currency": "USD"}]
    engine, _ = _engine(scripted=scripted, settings=Settings(max_retries=1))
    run = await engine.create_workflow(WorkflowGoal(title="Huge", description="transfer"))
    for _ in range(4):
        if run.status != RunStatus.AWAITING_APPROVAL:
            break
        engine.approvals.approve(run, "mgr", run.plan.plan_hash)
        run = await engine.resume_after_approval(run)
    assert run.status == RunStatus.FAILED
    assert run.attempt == 1
    assert run.error is not None and "exhausted" in run.error.lower()
