"""Round-4 regressions: C1 read-only snapshot boundary, C2 mandatory
dominates shrinkable, C3 mandatory evidence completeness."""

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
from trustflow.llm.usage import UsageTracker
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.retrieval.models import sha256_text
from trustflow.tools.registry import ToolRegistry


def _transfer(amount=50.0):
    return {"tool_name": "simulate_transfer", "from_account": "acc_alice",
            "to_account": "acc_bob", "amount": amount, "currency": "USD"}


class CountingProvider(FakeLLMProvider):
    def __init__(self, scripted):
        super().__init__(scripted=scripted)
        self.outage = False
        self.reviews = 0

    def propose_plan(self, *args, **kwargs):
        steps, rec = super().propose_plan(*args, **kwargs)
        rec.input_tokens, rec.output_tokens = 7, 11
        return steps, rec

    def semantic_review(self, payload):
        self.reviews += 1
        raw, rec = super().semantic_review(payload)
        rec.input_tokens, rec.output_tokens = 7, 11
        if self.outage:
            raise LLMResponseError("injected outage", record=rec)
        return raw, rec


def _engine(scripted, settings=None, retriever=None):
    registry = ToolRegistry()
    settings = settings or Settings(max_retries=1)
    policy = PolicyEngine(settings)
    provider = CountingProvider(scripted)
    retriever = retriever if retriever is not None else PolicyRetriever()
    engine = WorkflowEngine(
        Planner(provider, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(provider, registry.ledger, retriever=retriever),
        retriever=retriever)
    return engine, registry, provider


def _goal(required=True, title="transfer approval verification"):
    return WorkflowGoal(title=title,
                        description="transfer approval verification evidence",
                        require_evidence=required)


def _meta(requests, retry=False):
    def handle(request):
        requests.append(json.loads(request.content))
        if retry and len(requests) == 1:
            return httpx.Response(429, json={
                "usage": {"input_tokens": 7, "output_tokens": 11}})
        return httpx.Response(200, json={
            "output_text": json.dumps({"verdict": "PASS", "confidence": 1,
                                       "issues": [], "summary": "offline"}),
            "usage": {"input_tokens": 7, "output_tokens": 11}})

    settings = Settings(llm_mode="fake", model_api_key="offline-placeholder",
                        llm_base_url="https://offline.invalid",
                        llm_max_retries=int(retry), llm_backoff_seconds=0)
    return MetaResponsesProvider(settings, transport=httpx.MockTransport(handle))


# C1: duplicate canonical identity in a read-only workflow.

@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.asyncio
async def test_c1_read_only_duplicate_snapshot_rejected(stored):
    engine, registry, provider = _engine(
        [{"tool_name": "list_transactions", "account_id": "acc_alice",
          "limit": 1}])
    registry.ledger.transfer("acc_alice", "acc_bob", 1.0, "history")
    original = engine.semantic.ledger_snapshot

    def corrupt(plan):
        snap = original(plan)
        parsed = json.loads(snap.canonical_text)
        parsed["transactions"].append(dict(parsed["transactions"][0]))
        text = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        digest = sha256_text(text)
        return snap.model_copy(update={
            "canonical_text": text, "snapshot_hash": digest,
            "snapshot_id": "ledger-" + digest[:16],
            "transactions": parsed["transactions"],
            "balances": parsed["balances"]})

    if stored:
        provider.outage = True
        run = await engine.create_workflow(_goal())
        assert run.status == RunStatus.VERIFICATION_PENDING
        run.ledger_snapshot = corrupt(run.plan).model_dump(mode="json")
        provider.outage = False
        run = await engine.retry_verification(run)
    else:
        engine.semantic.ledger_snapshot = corrupt
        run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    # The stored case already consumed its first (outage) review call;
    # the corrupted snapshot must not cause a second one.
    assert provider.reviews == (1 if stored else 0)


# C2: mandatory dominates shrinkable at transport retry.

@pytest.mark.parametrize("mandatory,expected_requests", [(True, 1), (False, 2)])
def test_c2_mandatory_dominates_shrinkable(mandatory, expected_requests):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(429, json={
                "usage": {"input_tokens": 7, "output_tokens": 11}})
        return httpx.Response(200, json={
            "output_text": json.dumps({"steps": [_transfer()]}),
            "usage": {"input_tokens": 7, "output_tokens": 11}})

    settings = Settings(llm_mode="fake", model_api_key="offline-placeholder",
                        llm_base_url="https://offline.invalid",
                        llm_max_retries=1, llm_backoff_seconds=0)
    provider = MetaResponsesProvider(settings, transport=httpx.MockTransport(handle))
    budget = (len("transfer") + 2000) // 4 + 256 + 1200
    tracker = UsageTracker(budget)
    import contextlib
    with pytest.raises(Exception) if mandatory else contextlib.nullcontext() as exc:
        Planner(provider).create_plan(
            WorkflowGoal(title="transfer"), tracker=tracker,
            retrieval_context="x" * 4000, retrieval_mandatory=mandatory,
            retrieval_shrinkable=True)
    assert len(requests) == expected_requests
    if mandatory:
        assert exc is not None


# C3: aged operation, executed read rows, metadata completeness.

@pytest.mark.asyncio
async def test_c3_aged_operation_kept_after_51_later_transfers():
    engine, registry, provider = _engine(
        [_transfer()], settings=Settings(max_retries=1,
                                         token_budget_per_workflow=50000))
    provider.outage = True
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    current = registry.ledger.find_by_key(run.intents[0].intent_id)
    for i in range(51):
        registry.ledger.transfer("acc_alice", "acc_bob", 1.0, f"other-{i}")
    requests = []
    engine.semantic._llm = _meta(requests)
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 52
    sent = json.loads(requests[0]["input"].split("\n", 1)[1])
    assert current.tx_id in {t["tx_id"] for t in sent["evidence"]["transactions"]}


@pytest.mark.asyncio
async def test_c3_read_before_transfer_keeps_executed_rows():
    engine, registry, provider = _engine(
        [{"tool_name": "list_transactions", "account_id": "acc_alice",
          "limit": 35}, _transfer()],
        settings=Settings(max_retries=1, token_budget_per_workflow=50000),
        retriever=PolicyRetriever(corpus=[]))
    for i in range(35):
        registry.ledger.transfer("acc_alice", "acc_bob", 1.0, f"history-{i}")
    executed = {t.tx_id for t in registry.ledger.list_transactions("acc_alice", 35)}
    requests = []
    engine.semantic._llm = _meta(requests)
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED and len(requests) == 1
    sent = json.loads(requests[0]["input"].split("\n", 1)[1])
    assert executed <= {t["tx_id"] for t in sent["evidence"]["transactions"]}


@pytest.mark.parametrize("omit", ["empty", "partial"])
def test_c3_metadata_cannot_drop_required_rows(omit):
    requests = []
    provider = _meta(requests, retry=True)
    txs = [dict(tx_id=f"tx_{i:04d}", from_account="acc_alice",
                to_account="acc_bob", amount=1, currency="USD",
                idempotency_key="history-" + str(i) + "x" * 100)
           for i in range(35)]
    payload = {"goal": {"title": "read", "description": ""},
               "plan_steps": [{"tool_name": "list_transactions",
                               "args": {"account_id": "acc_alice",
                                        "limit": 35}}],
               "evidence": {"balances": {}, "transactions": txs},
               "mandatory_tx_ids": [] if omit == "empty" else [txs[0]["tx_id"]]}
    try:
        provider.semantic_review(payload)
    except LLMResponseError:
        return  # controlled typed failure before/without full HTTP is valid
    assert requests
    for req in requests:
        sent = json.loads(req["input"].split("\n", 1)[1])
        assert {t["tx_id"] for t in sent["evidence"]["transactions"]} == \
            {t["tx_id"] for t in txs}
