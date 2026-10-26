"""Regression tests for the independent M1 review findings (4d2bc2d)."""

import threading

import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.errors import LLMResponseError
from trustflow.core.logging import redact_any
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import PLAN_SCHEMA, FakeLLMProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry

SECRET = "api_key=SYNTH-SECRET-MARKER-1"


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
        semantic=SemanticReviewer(provider, registry.ledger) if semantic else None,
    )
    return engine, registry


def _transfer(amount, frm="acc_alice", to="acc_bob"):
    return {"tool_name": "simulate_transfer", "from_account": frm,
            "to_account": to, "amount": amount, "currency": "USD"}


# --- Finding 3/4: operation identity and revision reconciliation ---

@pytest.mark.asyncio
async def test_two_amounts_same_route_both_execute():
    fake = FakeLLMProvider(scripted=[_transfer(10.0), _transfer(20.0)])
    engine, registry = _engine(fake=fake)
    run = await engine.create_workflow(WorkflowGoal(title="two", description="pay twice"))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 2


@pytest.mark.asyncio
async def test_two_identical_intents_both_execute():
    fake = FakeLLMProvider(scripted=[_transfer(10.0), _transfer(10.0)])
    engine, registry = _engine(fake=fake)
    run = await engine.create_workflow(WorkflowGoal(title="two same", description="pay"))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 2


@pytest.mark.asyncio
async def test_revision_after_commit_completes_with_one_tx():
    """Commit once, fail verification once, revise, complete: exactly 1 tx."""
    state = {"fail_once": True}
    fake = FakeLLMProvider(scripted=[
        {"tool_name": "get_balance", "account_id": "acc_alice"}, _transfer(100.0),
    ])
    engine, registry = _engine(fake=fake)
    original_verify = engine.verifier.verify

    def flaky_verify(run, plan):
        report = original_verify(run, plan)
        if state["fail_once"] and report.passed:
            state["fail_once"] = False
            report.passed = False
            report.issues.append("one-time verifier failure (test)")
        return report

    engine.verifier.verify = flaky_verify  # type: ignore[method-assign]
    engine.semantic = None  # isolate deterministic revision path
    run = await engine.create_workflow(WorkflowGoal(title="t100", description="transfer 100"))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    # Current (revised) plan steps must have valid current results.
    assert run.plan is not None
    by_id = {r.step_id: r.status for r in run.step_results}
    assert all(by_id.get(s.step_id) is not None for s in run.plan.steps)


# --- Finding 5: late commit after timeout ---

@pytest.mark.asyncio
async def test_delayed_commit_after_timeout_reconciles_without_duplicate():
    gate = threading.Event()
    registry = ToolRegistry()
    s = Settings(max_retries=0, step_timeout_seconds=0.05)
    policy = PolicyEngine(s)
    engine = WorkflowEngine(
        Planner(FakeLLMProvider(), policy), Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
    )
    original = registry.invoke

    def gated(name, args, key=""):
        if name == "simulate_transfer":
            gate.wait(timeout=5)  # commit only after the step timeout fired
        return original(name, args, key)

    registry.invoke = gated  # type: ignore[method-assign]
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob")
    )
    # Outcome is uncertain, never a definitive execution failure yet.
    assert run.status == RunStatus.EXECUTION_UNCERTAIN
    assert len(registry.ledger.transactions) == 0
    gate.set()
    await engine.reconcile(run)
    # Worker may need a moment to commit after the gate opens.
    import asyncio as _asyncio

    for _ in range(100):
        if run.status == RunStatus.COMPLETED:
            break
        await _asyncio.sleep(0.02)
        await engine.reconcile(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1


# --- Finding 2: semantic validation and recoverable verification ---

@pytest.mark.asyncio
async def test_malformed_semantic_output_lands_in_recoverable_state():
    fake = FakeLLMProvider(review={"verdict": "MAYBE", "confidence": "high",
                                   "issues": "not-a-list"})
    engine, _ = _engine(fake=fake)
    run = await engine.create_workflow(WorkflowGoal(title="Check", description="balance"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    # Deterministic report was persisted before semantic review ran.
    assert run.verification is not None
    assert run.verification.passed is True


@pytest.mark.asyncio
async def test_semantic_retry_is_verification_only_no_new_transfer():
    fake = FakeLLMProvider(review={"verdict": "FAIL", "confidence": 0.9,
                                   "issues": ["temporary reviewer disagreement"]})
    engine, registry = _engine(fake=fake)
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob")
    )
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1  # committed, not rolled back
    fake._review = {"verdict": "PASS", "confidence": 0.9, "issues": []}
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1


def test_semantic_output_validated_explicitly():
    fake = FakeLLMProvider(review={"verdict": "MAYBE", "issues": []})
    reviewer = SemanticReviewer(fake, ToolRegistry().ledger)
    from trustflow.domain.models import Plan

    with pytest.raises(LLMResponseError):
        reviewer.review(WorkflowGoal(title="t"), Plan(goal_title="t", steps=[]))


# --- Finding 6: usage attribution ---

@pytest.mark.asyncio
async def test_failed_planning_usage_does_not_leak_into_next_run():
    bad = FakeLLMProvider(scripted=[{"tool_name": "nope"}])
    engine, _ = _engine(fake=bad)
    run1 = await engine.create_workflow(WorkflowGoal(title="bad", description="bad"))
    assert run1.status == RunStatus.FAILED
    assert len(run1.llm_calls) >= 1  # failed planning usage preserved on its run
    engine.planner._llm = FakeLLMProvider()
    run2 = await engine.create_workflow(WorkflowGoal(title="Check", description="balance"))
    assert run2.status == RunStatus.COMPLETED
    # Only run2's own calls (plan + semantic review), nothing leaked from run1.
    assert len(run2.llm_calls) == len([c for c in run2.llm_calls])
    assert all(c["purpose"] in ("plan", "semantic_review") for c in run2.llm_calls)
    assert len(run2.llm_calls) <= 2


# --- Finding 7: config and schema contracts ---

def test_llm_mode_contract():
    assert Settings(llm_mode="fake").llm_mode == "fake"
    assert Settings(llm_mode="LIVE").llm_mode == "live"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(llm_mode="fakee")


def test_plan_schema_covers_fx_fields():
    props = PLAN_SCHEMA["properties"]["steps"]["items"]["properties"]
    assert "from_currency" in props and "to_currency" in props


def test_tool_definitions_validate_against_arg_models():
    registry = ToolRegistry()
    samples = {
        "get_balance": {"account_id": "acc_alice"},
        "list_transactions": {"account_id": "acc_alice", "limit": 5},
        "simulate_transfer": {"from_account": "acc_alice", "to_account": "acc_bob",
                              "amount": 1.0, "currency": "USD"},
        "fx_quote": {"from_currency": "USD", "to_currency": "EUR", "amount": 1.0},
    }
    for definition in registry.tool_definitions():
        model = registry.get(definition["name"]).args_model
        model.model_validate(samples[definition["name"]])


# --- Finding 8: sanitization, frozen snapshot, independent verifier ---

@pytest.mark.asyncio
async def test_concurrent_runs_keep_usage_isolated():
    import asyncio as _asyncio

    engine, _ = _engine()
    runs = await _asyncio.gather(*[
        engine.create_workflow(WorkflowGoal(title=f"Check {i}", description="balance"))
        for i in range(3)
    ])
    assert all(r.status == RunStatus.COMPLETED for r in runs)
    ids = {r.run_id for r in runs}
    assert len(ids) == 3
    for r in runs:
        assert all(c["workflow_id"] == r.run_id for c in r.llm_calls)


@pytest.mark.asyncio
async def test_over_budget_response_remains_in_accounting():
    from trustflow.llm.usage import LLMCallRecord

    class BigUsage(FakeLLMProvider):
        def propose_plan(self, title, description, repair_feedback=None):
            steps, _ = super().propose_plan(title, description, repair_feedback)
            return steps, LLMCallRecord(purpose="plan", model="muse-wonder-1.0",
                                        input_tokens=9000, output_tokens=9000)

    s = Settings(max_retries=0, token_budget_per_workflow=100,
                 max_output_tokens=50)
    engine, _ = _engine(fake=BigUsage(), settings=s)
    run = await engine.create_workflow(WorkflowGoal(title="Check", description="balance"))
    assert run.status == RunStatus.FAILED
    assert len(run.llm_calls) == 1  # incurred record preserved, not removed
    assert run.llm_calls[0]["over_budget"] is True


def test_redact_any_recurses_into_lists():
    payload = {"a": [{"b": SECRET}, ["token: SYNTH-SECRET-MARKER-2"]], "ok": "fine"}
    cleaned = redact_any(payload)
    assert "SYNTH-SECRET" not in str(cleaned)
    assert cleaned["ok"] == "fine"


@pytest.mark.asyncio
async def test_approved_snapshot_isolated_from_later_mutation():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 5000", description="transfer 5000 to bob")
    )
    assert run.plan is not None
    engine.approvals.approve(run, "mgr", run.plan.plan_hash)
    # Mutating the live plan object after approval must not change execution.
    for step in run.plan.steps:
        if step.tool_name == "simulate_transfer":
            step.args["amount"] = 1.0
    run = await engine.resume_after_approval(run)
    assert registry.ledger.get_balance("acc_bob").balance == 1200.0 + 5000.0


@pytest.mark.asyncio
async def test_verifier_reads_ledger_not_tool_claims():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob")
    )
    assert run.status == RunStatus.COMPLETED
    # Forge a tool-result claim: verifier must reject it against the ledger.
    forged = next(r for r in run.step_results if r.tool_name == "simulate_transfer")
    forged.data = {"tx_id": "tx_forged"}
    assert run.plan is not None
    report = engine.verifier.verify(run, run.plan)
    assert report.passed is False
