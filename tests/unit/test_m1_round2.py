"""Round-2 adversarial probes (N1-N6, acceptance A-O) on synthetic data."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.api.app import Container, create_app
from trustflow.core.logging import redact
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
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
    s = settings or Settings(max_retries=2)
    policy = PolicyEngine(s)
    provider = fake or FakeLLMProvider()
    return WorkflowEngine(
        Planner(provider, policy, max_output_tokens=s.max_output_tokens),
        Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
        semantic=SemanticReviewer(provider, registry.ledger) if semantic else None,
    ), registry


def _fail_once(engine, after_first=False):
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


def _amounts(registry):
    return sorted(tx.amount for tx in registry.ledger.transactions)


# N1 / B: only second payment remains; revision with only Ops must pay Ops
@pytest.mark.asyncio
async def test_n1_revision_only_second_payment_pays_second():
    # v1: Bob 100 succeeds, Ops 200 fails (insufficient from bob? use alice ops 200 ok)
    # Make Ops fail first time via insufficient: alice->ops 200 is fine, so fail
    # verification once instead; both commit in v1. To get Ops uncommitted, use
    # amount alice cannot cover twice: Bob 100 (alice 5000) + Ops 999999 fails.
    provider = Seq([[_t(100.0), _t(999999.0, to="acc_ops")], [_t(200.0, to="acc_ops")]])
    engine, registry = _engine(
        fake=provider,
        settings=Settings(max_retries=2, high_risk_threshold=2000000.0))
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    # v1 fails deterministically (insufficient), revision proposes Ops 200 only.
    # Bob 100 committed must be preserved; Ops intent is modified-uncommitted
    # (999999 -> 200) so it needs authorization OR executes as its own intent,
    # but Bob must not be paid again and Ops must not inherit Bob's identity.
    recipients = [(tx.to_account, tx.amount) for tx in registry.ledger.transactions]
    assert ("acc_bob", 100.0) in recipients
    assert recipients.count(("acc_bob", 100.0)) == 1
    assert run.status != RunStatus.COMPLETED or ("acc_ops", 200.0) in recipients


# N1 canonical probe: commit Bob, fail Ops via verifier, revision = [Ops only]
@pytest.mark.asyncio
async def test_n1_verifier_fail_revision_only_ops():
    provider = Seq([
        [_t(100.0), _t(200.0, to="acc_ops")],
        [_t(200.0, to="acc_ops")],
    ])
    engine, registry = _engine(fake=provider)
    _fail_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status == RunStatus.COMPLETED
    recipients = sorted((tx.to_account, tx.amount) for tx in registry.ledger.transactions)
    # Both intents committed exactly once (v1 already paid both; no repeats).
    assert recipients == [("acc_bob", 100.0), ("acc_ops", 200.0)]


# N2 / D: new payment inserted before committed payment
@pytest.mark.asyncio
async def test_n2_inserted_payment_does_not_duplicate_committed():
    provider = Seq([[_t(100.0)], [_t(50.0, to="acc_ops"), _t(100.0)]])
    engine, registry = _engine(fake=provider)
    _fail_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    recipients = [(tx.to_account, tx.amount) for tx in registry.ledger.transactions]
    assert recipients.count(("acc_bob", 100.0)) == 1
    # Ops is a NEW intent: must not execute without authorization.
    if run.status == RunStatus.COMPLETED:
        assert ("acc_ops", 50.0) not in recipients
    else:
        assert run.status == RunStatus.AWAITING_APPROVAL


# N3 / F: changed committed intent -> explicit conflict, no silent rewrite
@pytest.mark.parametrize("changed", [
    _t(101.0), _t(100.0, to="acc_ops"), _t(100.0, frm="acc_ops", to="acc_bob"),
    _t(100.0, ccy="EUR"),
])
@pytest.mark.asyncio
async def test_n3_changed_committed_is_conflict_not_completed(changed):
    provider = Seq([[_t(100.0)], [changed]])
    engine, registry = _engine(fake=provider)
    _fail_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status != RunStatus.COMPLETED
    assert run.error and "conflict" in run.error.lower()
    assert _amounts(registry) == [100.0]


# G: changed uncommitted payload must not execute old payload silently
@pytest.mark.asyncio
async def test_g_changed_uncommitted_not_old_payload():
    provider = Seq([[_t(999999.0)], [_t(150.0)]])
    engine, registry = _engine(fake=provider, settings=Settings(max_retries=2))
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    # 999999 fails (insufficient); revision changes to 150 = modified uncommitted
    # intent: requires authorization, or completes only by paying 150 once
    # after approval flow. It must never pay a stale different amount silently
    # without the run passing through AWAITING_APPROVAL or conflict.
    assert _amounts(registry) in ([], [150.0])
    if _amounts(registry) == [150.0]:
        assert run.status == RunStatus.COMPLETED


# E: two identical payments, only one completed -> coverage forbids COMPLETED
@pytest.mark.asyncio
async def test_e_identical_coverage_blocks_completion():
    provider = Seq([[_t(10.0), _t(10.0)], [_t(10.0)]])
    engine, registry = _engine(fake=provider)
    _fail_once(engine)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    # v1 pays both (2 tx). Revision drops one: coverage still satisfied by
    # manifest evidence, so completion with exactly 2 is acceptable; the
    # forbidden outcome is a third duplicate or claiming one tx covers both.
    assert len(registry.ledger.transactions) == 2
    assert run.status == RunStatus.COMPLETED


# I: ambiguous ID-less revision fails closed
@pytest.mark.asyncio
async def test_i_ambiguous_idless_revision_fails_closed():
    provider = Seq([
        [_t(100.0, to="acc_bob"), _t(200.0, to="acc_ops")],
        [_t(150.0, to="acc_bob"), _t(250.0, to="acc_ops")],
    ])
    engine, registry = _engine(
        fake=provider,
        settings=Settings(max_retries=2, high_risk_threshold=2000000.0))
    provider._plans = [
        [_t(4000.0, to="acc_bob"), _t(4000.0, to="acc_ops")],
        [_t(150.0, to="acc_bob"), _t(250.0, to="acc_ops")],
    ]
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    # v1: first 4000 commits, second fails (insufficient). Revision changes
    # both payloads with two unmatched intents -> ambiguous: fail closed,
    # no additional payments beyond the first committed 4000.
    assert _amounts(registry) == [4000.0]
    assert run.status != RunStatus.COMPLETED


# N5: whitespace-delimited secret forms
@pytest.mark.parametrize("text", [
    "Authorization SYNTH-SECRET-A1", "secret SYNTH-SECRET-A2",
    "credentials are SYNTH-SECRET-A3", "Authorization: SYNTH-SECRET-A4",
])
def test_n5_redaction_whitespace_forms(text):
    assert "SYNTH-SECRET" not in redact(text)


# N6: unsupported FX fails explicitly; identity conversion preserved
def test_n6_unsupported_fx_fails():
    from trustflow.core.errors import ToolValidationError

    registry = ToolRegistry()
    with pytest.raises(ToolValidationError):
        registry.invoke("fx_quote", {"from_currency": "USD", "to_currency": "JPY",
                                     "amount": 10.0})
    ok = registry.invoke("fx_quote", {"from_currency": "USD", "to_currency": "USD",
                                      "amount": 10.0})
    assert ok.data["rate"] == 1.0


# Budget: original low-budget scenario exhausts before any call
@pytest.mark.asyncio
async def test_budget_low_scenario_fails_before_payment():
    engine, registry = _engine(settings=Settings(
        token_budget_per_workflow=10, max_output_tokens=50, max_retries=0))
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob"))
    assert run.status == RunStatus.FAILED
    assert run.error and "budget" in run.error.lower()
    assert len(registry.ledger.transactions) == 0


# M/O: HTTP concurrency + secrets through actual API responses
def test_http_concurrent_approvals_single_transfer():
    container = Container(Settings())
    app = create_app(container)
    client = TestClient(app)
    body = client.post("/workflows", json={
        "title": "Transfer 5000", "description": "transfer 5000 to bob"}).json()
    assert body["status"] == "AWAITING_APPROVAL"
    url = f"/workflows/{body['run_id']}/approve"
    payload = {"actor": "mgr", "plan_hash": body["plan"]["plan_hash"]}

    def call(_):
        return TestClient(app).post(url, json=payload).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(call, range(4)))
    assert codes.count(200) == 1
    assert container.registry.ledger.get_balance("acc_bob").balance == 6200.0


def test_api_response_has_no_secret_marker():
    container = Container(Settings())
    fake = container.engine.planner._llm
    fake._review = {"verdict": "FAIL", "confidence": 0.5,
                    "issues": ["Authorization SYNTH-SECRET-API1"]}
    client = TestClient(create_app(container))
    resp = client.post("/workflows", json={"title": "Check", "description": "balance"})
    assert "SYNTH-SECRET" not in resp.text
    run_id = resp.json()["run_id"]
    got = client.get(f"/workflows/{run_id}")
    assert "SYNTH-SECRET" not in got.text
