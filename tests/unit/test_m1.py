import asyncio
import time

import httpx
import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner, validate_steps
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.errors import LLMResponseError, PlanValidationError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider, MetaResponsesProvider, build_provider
from trustflow.llm.usage import UsageTracker, estimate_cost
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry


def _engine(fake=None, settings=None, semantic=True):
    registry = ToolRegistry()
    s = settings or Settings(max_retries=2)
    policy = PolicyEngine(s)
    provider = fake or FakeLLMProvider()
    engine = WorkflowEngine(
        Planner(provider, policy), Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
        semantic=SemanticReviewer(provider, registry.ledger) if semantic else None,
    )
    return engine, registry


def test_plan_hash_covers_executable_args():
    from trustflow.domain.models import Plan, PlanStep

    a = Plan(goal_title="g", steps=[PlanStep(step_id="s", tool_name="simulate_transfer",
                                             args={"amount": 10})])
    b = Plan(goal_title="g", steps=[PlanStep(step_id="s", tool_name="simulate_transfer",
                                             args={"amount": 11})])
    assert a.plan_hash != b.plan_hash


def test_unknown_tool_fails_closed():
    with pytest.raises(PlanValidationError):
        validate_steps([{"tool_name": "wire_real_money", "amount": 1}])


def test_malformed_steps_fail_closed():
    with pytest.raises(PlanValidationError):
        validate_steps("not-a-list")


@pytest.mark.asyncio
async def test_one_repair_then_fail_closed():
    bad = [{"tool_name": "wire_real_money"}]
    engine, _ = _engine(fake=FakeLLMProvider(scripted=bad))
    run = await engine.create_workflow(WorkflowGoal(title="x", description="y"))
    assert run.status == RunStatus.FAILED
    assert engine.planner._llm.repair_calls == 1


@pytest.mark.asyncio
async def test_semantic_pass_never_overrides_deterministic_fail():
    fake = FakeLLMProvider(
        scripted=[{"tool_name": "get_balance", "account_id": "acc_missing"}],
        review={"verdict": "PASS", "confidence": 1.0, "issues": [], "summary": "ok"},
    )
    # acc_missing is rejected by plan validation, so use a failing execution path:
    fake._scripted = [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                       "to_account": "acc_bob", "amount": 999999.0}]
    engine, _ = _engine(fake=fake, settings=Settings(max_retries=0))
    run = await engine.create_workflow(WorkflowGoal(title="t", description="transfer"))
    if run.status == RunStatus.AWAITING_APPROVAL:
        engine.approvals.approve(run, "mgr", run.plan.plan_hash)
        run = await engine.resume_after_approval(run)
    assert run.status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_committed_transfer_never_repeated_after_verifier_failure():
    engine, registry = _engine(settings=Settings(max_retries=2))
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 500", description="transfer 500 to bob")
    )
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    # Force a replan cycle on the same run identity: execute the v1 plan again
    # through the executor; the operation key blocks any second transfer.
    assert run.plan is not None
    await engine.executor.execute(run, run.plan)
    assert len(registry.ledger.transactions) == 1


@pytest.mark.asyncio
async def test_timeout_after_mutation_reconciles():
    registry = ToolRegistry()
    s = Settings(max_retries=0, step_timeout_seconds=0.05)
    policy = PolicyEngine(s)
    engine = WorkflowEngine(
        Planner(FakeLLMProvider(), policy), Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
    )
    original = registry.invoke

    def slow_invoke(name, args, key=""):
        out = original(name, args, key)  # mutation commits here
        time.sleep(0.3)  # then response is delayed past the step timeout
        return out

    registry.invoke = slow_invoke  # type: ignore[method-assign]
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob")
    )
    # Stabilized contract: timeout is UNCERTAIN (never definitive failure),
    # then reconcile resolves the committed worker without duplicating it.
    from trustflow.domain.models import RunStatus as _RS

    assert run.status == _RS.EXECUTION_UNCERTAIN
    import asyncio as _asyncio

    for _ in range(100):
        run = await engine.reconcile(run)
        if run.status == _RS.COMPLETED:
            break
        await _asyncio.sleep(0.02)
    assert run.status == _RS.COMPLETED
    assert len(registry.ledger.transactions) == 1
    transfer_results = [r for r in run.step_results if r.tool_name == "simulate_transfer"]
    assert transfer_results and transfer_results[0].data.get("reconciled_from_ledger")


def test_cost_estimates_use_official_rates():
    assert estimate_cost("muse-wonder-1.0", 1_000_000, 0) == 4.0
    assert estimate_cost("muse-spark-1.3", 0, 1_000_000) == 4.25


def test_token_budget_fails_closed():
    from trustflow.core.errors import TokenBudgetExceededError
    from trustflow.llm.usage import LLMCallRecord

    tracker = UsageTracker(budget_tokens=10)
    with pytest.raises(TokenBudgetExceededError):
        tracker.record(LLMCallRecord(purpose="plan", model="fake",
                                     input_tokens=11, output_tokens=0))


def test_live_provider_malformed_response_no_fake_fallback(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-placeholder-not-a-real-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp_1", "output_text": "not json",
                                         "usage": {"input_tokens": 3, "output_tokens": 2}})

    provider = MetaResponsesProvider(
        Settings(llm_max_retries=0), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMResponseError):
        provider.propose_plan("goal", "desc")


def test_live_mode_without_key_raises(monkeypatch):
    from trustflow.core.errors import LLMNotConfiguredError

    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        build_provider(Settings(llm_mode="live", llm_api_key=None))


def test_live_provider_parses_usage_and_request_id(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-placeholder-not-a-real-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={
            "id": "resp_42", "output_text": '{"steps": []}',
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    provider = MetaResponsesProvider(
        Settings(llm_max_retries=0), transport=httpx.MockTransport(handler)
    )
    steps, record = provider.propose_plan("goal", "desc")
    assert steps == []
    assert record.request_id == "resp_42"
    assert record.input_tokens == 10 and record.output_tokens == 5


def test_asyncio_loop_available():
    asyncio.get_event_loop_policy()
