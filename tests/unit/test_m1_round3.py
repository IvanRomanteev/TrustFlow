"""Round-3 adversarial tests: cancellation, foreign evidence, lifecycle, secrets."""

import asyncio
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.api.app import Container, create_app
from trustflow.core.logging import redact
from trustflow.core.settings import Settings
from trustflow.domain.models import IntentStatus, RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider
from trustflow.llm.usage import LLMCallRecord
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry


def _t(amount, frm="acc_alice", to="acc_bob", ccy="USD"):
    return {"tool_name": "simulate_transfer", "from_account": frm,
            "to_account": to, "amount": amount, "currency": ccy}


class Seq(FakeLLMProvider):
    def __init__(self, plans, review=None):
        super().__init__(review=review)
        self._plans, self._i = plans, 0

    def propose_plan(self, title, description, repair_feedback=None):
        idx = min(self._i, len(self._plans) - 1)
        self._i += 1
        return [dict(s) for s in self._plans[idx]], LLMCallRecord(purpose="plan", model="fake")


def _engine(fake=None, settings=None, semantic=False):
    registry = ToolRegistry()
    s = settings or Settings(max_retries=2, high_risk_threshold=2000000.0)
    policy = PolicyEngine(s)
    provider = fake or FakeLLMProvider()
    return WorkflowEngine(
        Planner(provider, policy, max_output_tokens=s.max_output_tokens),
        Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
        semantic=SemanticReviewer(provider, registry.ledger) if semantic else None,
    ), registry


def _fail_once(engine):
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


def _txs(registry):
    return sorted((tx.to_account, tx.amount) for tx in registry.ledger.transactions)


# A: omit AUTHORIZED pending B (B failed for funds, A committed)
@pytest.mark.asyncio
async def test_a_omit_authorized_pending_is_conflict_not_cancelled():
    # Bob pays Alice->Bob 100 (commits); Ops from Bob 1000 fails (Bob has 1300? no:
    # Bob starts 1200, receives 100 -> 1300, so use 5000 to fail deterministically)
    provider = Seq([
        [_t(100.0), _t(5000.0, frm="acc_bob", to="acc_ops")],
        [_t(100.0)],  # revision omits B entirely
    ])
    engine, registry = _engine(fake=provider)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status == RunStatus.FAILED
    assert run.error and "conflict" in run.error.lower()
    assert _txs(registry) == [("acc_bob", 100.0)]
    b = [i for i in run.intents if i.authorized_payload.get("to_account") == "acc_ops"]
    assert b and b[0].status != IntentStatus.CANCELLED
    actions = [e.action for e in engine.audit.list(run.run_id)]
    assert "verification_finished" in actions


# B: revision replaced entirely with get_balance
@pytest.mark.asyncio
async def test_b_revision_only_balance_keeps_obligation():
    provider = Seq([
        [_t(100.0), _t(5000.0, frm="acc_bob", to="acc_ops")],
        [{"tool_name": "get_balance", "account_id": "acc_alice"}],
    ])
    engine, registry = _engine(fake=provider)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status == RunStatus.FAILED
    assert _txs(registry) == [("acc_bob", 100.0)]


# C: two identical, second pending and omitted
@pytest.mark.asyncio
async def test_c_identical_second_pending_omitted_blocks_completion():
    provider = Seq([
        [_t(1000.0, frm="acc_bob", to="acc_alice"),
         _t(1000.0, frm="acc_bob", to="acc_ops")],
        [_t(1000.0, frm="acc_bob", to="acc_alice")],
    ])
    engine, registry = _engine(fake=provider)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    # First commits (Bob 1200->200), second fails (insufficient). Revision
    # omits the second: it must not be cancelled into a COMPLETED run.
    assert run.status != RunStatus.COMPLETED
    assert _txs(registry) == [("acc_alice", 1000.0)]


# D: omit only a PROPOSED (never authorized) operation may withdraw
@pytest.mark.asyncio
async def test_d_omit_proposed_may_withdraw():
    from trustflow.domain.models import Plan, PlanStep, WorkflowRun
    from trustflow.orchestration.engine import assign_operation_ids

    run = WorkflowRun(goal=WorkflowGoal(title="t"))
    plan1 = Plan(goal_title="t", steps=[
        PlanStep(step_id="s1", tool_name="simulate_transfer", args=_t(50.0))])
    assign_operation_ids(run, plan1, new_intents_authorized=False)
    assert run.intents[0].status == IntentStatus.PROPOSED
    plan2 = Plan(goal_title="t", steps=[
        PlanStep(step_id="s1", tool_name="get_balance", args={"account_id": "acc_alice"})])
    assign_operation_ids(run, plan2, new_intents_authorized=False)
    assert run.intents[0].status == IntentStatus.CANCELLED


# F: forged cancellation (manual CANCELLED on authorized intent) does not verify
@pytest.mark.asyncio
async def test_f_forged_cancellation_does_not_satisfy_verifier():
    provider = Seq([[_t(5000.0, frm="acc_bob", to="acc_ops")]])
    engine, registry = _engine(fake=provider)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.intents and run.intents[0].status == IntentStatus.AUTHORIZED
    run.intents[0].status = IntentStatus.CANCELLED  # forged, no authorization evidence
    assert run.plan is not None
    report = engine.verifier.verify(run, run.plan)
    assert report.passed is False


# G/I: late tx for cancelled intent must not resurrect it via sync
@pytest.mark.asyncio
async def test_g_late_commit_does_not_resurrect_cancelled():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob"))
    assert run.status == RunStatus.COMPLETED
    intent = run.intents[0]
    intent.status = IntentStatus.CANCELLED  # simulate prior cancellation record
    engine.verifier.sync_intents(run)
    assert intent.status == IntentStatus.CANCELLED
    report = engine.verifier.verify(run, run.plan)
    assert report.passed is False  # integrity conflict surfaced, not hidden


# H: foreign ledger evidence on a PROPOSED intent must fail verification
@pytest.mark.asyncio
async def test_h_foreign_evidence_rejected():
    engine_a, registry = _engine()
    run_a = await engine_a.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob"))
    assert run_a.status == RunStatus.COMPLETED
    foreign_tx = registry.ledger.transactions[0]
    # Second workflow on the SAME ledger with a pending (PROPOSED) intent.
    engine_b = WorkflowEngine(
        engine_a.planner, Executor(registry, Settings()),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        settings=Settings(),
    )
    provider = Seq([[_t(5000.0)]])
    engine_b.planner = Planner(provider, PolicyEngine(Settings()))
    run_b = await engine_b.create_workflow(WorkflowGoal(title="pay", description="pay"))
    # Forge step evidence pointing at workflow A's transaction.
    from trustflow.domain.models import StepResult, StepStatus

    step = run_b.plan.steps[0] if run_b.plan else None
    assert step is not None
    run_b.step_results = [StepResult(
        step_id=step.step_id, tool_name="simulate_transfer",
        status=StepStatus.SUCCEEDED, data={"tx_id": foreign_tx.tx_id},
        idempotency_key=foreign_tx.idempotency_key)]
    report = engine_b.verifier.verify(run_b, run_b.plan)
    assert report.passed is False


# J: nested secret forms, incl. "the Authorization header value X"
@pytest.mark.parametrize("text", [
    "the Authorization header value SYNTH-SECRET-A8",
    "nested {inner: Authorization SYNTH-SECRET-A9}",
    "provider said: credentials are SYNTH-SECRET-B1, retry later",
])
def test_j_secret_value_patterns(text):
    assert "SYNTH-SECRET" not in redact(text)


def test_j_api_semantic_secret_not_exposed():
    container = Container(Settings())
    container.engine.planner._llm._review = {
        "verdict": "FAIL", "confidence": 0.5,
        "issues": ["the Authorization header value SYNTH-SECRET-API8"]}
    client = TestClient(create_app(container))
    resp = client.post("/workflows", json={"title": "Check", "description": "balance"})
    assert "SYNTH-SECRET" not in resp.text
    got = client.get(f"/workflows/{resp.json()['run_id']}")
    assert "SYNTH-SECRET" not in got.text


# K: multiple revisions preserve outstanding obligations
@pytest.mark.asyncio
async def test_k_multiple_revisions_preserve_obligation():
    provider = Seq([
        [_t(100.0), _t(5000.0, frm="acc_bob", to="acc_ops")],
        [_t(100.0)],
        [_t(100.0)],
    ])
    engine, registry = _engine(fake=provider, settings=Settings(
        max_retries=5, high_risk_threshold=2000000.0))
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status == RunStatus.FAILED
    assert _txs(registry) == [("acc_bob", 100.0)]
    ops = [i for i in run.intents if i.proposed_payload.get("to_account") == "acc_ops"
           or i.authorized_payload.get("to_account") == "acc_ops"]
    assert ops and ops[0].status not in (IntentStatus.CANCELLED, IntentStatus.COMMITTED)


# L: real HTTP (uvicorn) concurrent approvals -> exactly one transfer
def test_l_real_http_concurrent_approvals():
    container = Container(Settings())
    config = uvicorn.Config(create_app(container), host="127.0.0.1", port=0,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        body = httpx.post(f"{base}/workflows", json={
            "title": "Transfer 5000", "description": "transfer 5000 to bob"},
            timeout=10).json()
        assert body["status"] == "AWAITING_APPROVAL"
        url = f"{base}/workflows/{body['run_id']}/approve"
        payload = {"actor": "mgr", "plan_hash": body["plan"]["plan_hash"]}

        async def fire():
            async with httpx.AsyncClient() as client:
                return await asyncio.gather(*[
                    client.post(url, json=payload, timeout=10) for _ in range(4)
                ])

        responses = asyncio.run(fire())
        codes = [r.status_code for r in responses]
        assert codes.count(200) == 1
        assert container.registry.ledger.get_balance("acc_bob").balance == 6200.0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
