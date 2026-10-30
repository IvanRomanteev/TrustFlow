"""Round-5 regression: C4 provider must not promote later history
into mandatory executed-read evidence."""

import json

import httpx
import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.errors import LLMResponseError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider, MetaResponsesProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.tools.registry import ToolRegistry


def _transfer(amount=50.0):
    return {"tool_name": "simulate_transfer", "from_account": "acc_alice",
            "to_account": "acc_bob", "amount": amount, "currency": "USD"}


def _read(limit=35):
    return {"tool_name": "list_transactions", "account_id": "acc_alice",
            "limit": limit}


class CountingProvider(FakeLLMProvider):
    def __init__(self, scripted):
        super().__init__(scripted=scripted)
        self.outage = False

    def propose_plan(self, *args, **kwargs):
        steps, rec = super().propose_plan(*args, **kwargs)
        rec.input_tokens, rec.output_tokens = 7, 11
        return steps, rec

    def semantic_review(self, payload):
        raw, rec = super().semantic_review(payload)
        rec.input_tokens, rec.output_tokens = 7, 11
        if self.outage:
            raise LLMResponseError("injected outage", record=rec)
        return raw, rec


def _engine(scripted, settings=None):
    registry = ToolRegistry()
    settings = settings or Settings(max_retries=1,
                                    token_budget_per_workflow=50000)
    policy = PolicyEngine(settings)
    provider = CountingProvider(scripted)
    retriever = PolicyRetriever()
    engine = WorkflowEngine(
        Planner(provider, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(provider, registry.ledger, retriever=retriever),
        retriever=retriever)
    return engine, registry, provider


def _meta(requests):
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "output_text": json.dumps({"verdict": "PASS", "confidence": 1,
                                       "issues": [], "summary": "offline"}),
            "usage": {"input_tokens": 7, "output_tokens": 11}})

    settings = Settings(llm_mode="fake", model_api_key="offline-placeholder",
                        llm_base_url="https://offline.invalid", llm_max_retries=0)
    return MetaResponsesProvider(settings, transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("with_transfer", [False, True])
@pytest.mark.asyncio
async def test_c4_growth_recovery_keeps_exact_obligations(with_transfer):
    steps = [_read()] + ([_transfer()] if with_transfer else [])
    engine, registry, provider = _engine(steps)
    for i in range(35):
        registry.ledger.transfer("acc_alice", "acc_bob", 1.0, f"history-{i}")
    provider.outage = True
    run = await engine.create_workflow(WorkflowGoal(
        title="transfer approval verification",
        description="transfer approval verification evidence",
        require_evidence=True))
    assert run.status == RunStatus.VERIFICATION_PENDING
    required = {t["tx_id"] for r in run.step_results
                for t in r.data.get("transactions", [])}
    if with_transfer:
        required.add(registry.ledger.find_by_key(
            run.intents[0].intent_id).tx_id)
    total_after_a = len(registry.ledger.transactions)
    for i in range(51):
        registry.ledger.transfer("acc_alice", "acc_bob", 1.0, f"later-{i}")
    requests = []
    engine.semantic._llm = _meta(requests)
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == total_after_a + 51
    assert len(requests) == 1
    sent = json.loads(requests[0]["input"].split("\n", 1)[1])
    sent_ids = {t["tx_id"] for t in sent["evidence"]["transactions"]}
    # Mandatory rows are exactly the original obligations (optional
    # later history may be pruned, never promoted to mandatory).
    assert sent_ids == required


def test_c4_provider_complete_hint_is_exact_incomplete_hint_protected():
    txs = [dict(tx_id=f"tx_{i:04d}", from_account="acc_alice",
                to_account="acc_bob", amount=1, currency="USD",
                idempotency_key="k" * 120 + str(i)) for i in range(40)]
    base = {"goal": {"title": "read", "description": ""},
            "plan_steps": [{"tool_name": "list_transactions",
                            "args": {"account_id": "acc_alice", "limit": 3}}],
            "evidence": {"balances": {}, "transactions": txs}}
    # Complete authoritative hint: use exactly it, no later-window
    # promotion (available rows stand in for the grown ledger here).
    requests = []
    provider = _meta(requests)
    complete = {**base, "mandatory_tx_ids": ["tx_0000", "tx_0001"],
              "mandatory_tx_ids_complete": True}
    provider.semantic_review(complete)
    sent = json.loads(requests[0]["input"].split("\n", 1)[1])
    assert {t["tx_id"] for t in sent["evidence"]["transactions"]} == \
        {"tx_0000", "tx_0001"}
    assert "mandatory_tx_ids" not in requests[0]["input"]
    assert "mandatory_tx_ids_complete" not in requests[0]["input"]
    # Without the completeness marker (direct/incomplete metadata),
    # C3 protection still applies: read rows cannot be silently lost.
    requests2 = []
    provider2 = _meta(requests2)
    provider2.semantic_review({**base, "mandatory_tx_ids": ["tx_0039"]})
    sent2 = json.loads(requests2[0]["input"].split("\n", 1)[1])
    assert {"tx_0037", "tx_0038", "tx_0039"} <= \
        {t["tx_id"] for t in sent2["evidence"]["transactions"]}
