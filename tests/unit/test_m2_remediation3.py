"""Round-3 residual regressions: N1, F1, F2, F3, F4 (review of 70e20f6)."""

import json

import httpx
import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider, MetaResponsesProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.retrieval.models import (
    LedgerSnapshot,
    PolicyDocument,
    build_ledger_snapshot,
    make_ledger_span,
    span_in_text,
)
from trustflow.tools.registry import ToolRegistry


def _transfer(amount=50.0):
    return {"tool_name": "simulate_transfer", "from_account": "acc_alice",
            "to_account": "acc_bob", "amount": amount, "currency": "USD"}


class CountingProvider(FakeLLMProvider):
    def __init__(self, scripted):
        super().__init__(scripted=scripted)
        self.reviews = 0
        self.payloads = []

    def propose_plan(self, *args, **kwargs):
        steps, rec = super().propose_plan(*args, **kwargs)
        rec.input_tokens, rec.output_tokens = 7, 11
        return steps, rec

    def semantic_review(self, payload):
        self.reviews += 1
        self.payloads.append(payload)
        raw, rec = super().semantic_review(payload)
        rec.input_tokens, rec.output_tokens = 7, 11
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


def _after_commit(engine, registry, action):
    execute = engine.executor.execute

    async def wrapped(run, plan):
        results = await execute(run, plan)
        action(run)
        return results

    engine.executor.execute = wrapped


# N1: malformed policy key after commit is typed recovery, not a crash.

@pytest.mark.parametrize("field", ["start", "end", "chunk_id", "text_hash"])
@pytest.mark.asyncio
async def test_n1_malformed_policy_key_bounded_recovery(field):
    engine, registry, provider = _engine([_transfer()])
    saved = []

    def corrupt(run):
        saved.extend(json.loads(json.dumps(run.evidence_spans)))
        run.evidence_spans[0][field] = []

    _after_commit(engine, registry, corrupt)
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert run.verification is not None and run.verification.passed
    assert len(registry.ledger.transactions) == 1 and provider.reviews == 0
    run.evidence_spans = saved
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1


# F1/F2: conflicting duplicate rows and wrong fresh snapshot id.

def _snapshot_from_parts(original, parsed, **updates):
    from trustflow.retrieval.models import sha256_text
    text = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    digest = sha256_text(text)
    return original.model_copy(update={
        "canonical_text": text, "snapshot_hash": digest,
        "snapshot_id": "ledger-" + digest[:16],
        "transactions": json.loads(json.dumps(parsed["transactions"])),
        "balances": json.loads(json.dumps(parsed["balances"])), **updates})


@pytest.mark.asyncio
async def test_f1_conflicting_duplicate_tx_id_rejected():
    engine, registry, provider = _engine([_transfer()])
    original = engine.semantic.ledger_snapshot

    def ambiguous(plan):
        snap = original(plan)
        parsed = json.loads(snap.canonical_text)
        foreign = {**parsed["transactions"][0], "amount": 99,
                   "from_account": "acc_ops", "idempotency_key": "foreign-op"}
        parsed["transactions"].insert(0, foreign)
        snap = LedgerSnapshot.model_validate(
            _snapshot_from_parts(snap, parsed).model_dump())
        return snap

    engine.semantic.ledger_snapshot = ambiguous
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1 and provider.reviews == 0


@pytest.mark.asyncio
async def test_f2_wrong_fresh_snapshot_id_rejected():
    engine, registry, provider = _engine([_transfer()])
    original = engine.semantic.ledger_snapshot
    engine.semantic.ledger_snapshot = lambda plan: original(plan).model_copy(
        update={"snapshot_id": "ledger-" + "0" * 16})
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1 and provider.reviews == 0


def test_f1_exact_row_citation_offsets():
    registry = ToolRegistry()
    registry.ledger.transfer("acc_alice", "acc_bob", 50.0, "op-current")
    snap = build_ledger_snapshot(registry.ledger)
    start, end = span_in_text(snap.canonical_text, '"tx_id":"tx_0001"')
    span = make_ledger_span(snap, start, end)
    row = json.loads(snap.canonical_text)["transactions"][0]
    row_text = json.dumps(row, sort_keys=True, separators=(",", ":"))
    assert snap.canonical_text.index(row_text) <= span.start
    assert span.end <= snap.canonical_text.index(row_text) + len(row_text)


# F3: optional context retry and the 1515 context-free fallback.

@pytest.mark.asyncio
async def test_f3_optional_context_transport_retry_budget_1800():
    doc = PolicyDocument(doc_id="large", version="1",
                         corpus_version="synthetic-policies-v1",
                         title="transfer",
                         text="transfer approval verification " * 80)
    engine, registry, provider = _engine(
        [_transfer()], settings=Settings(
            max_retries=1, token_budget_per_workflow=1800),
        retriever=PolicyRetriever(corpus=[doc]))
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
    engine.planner._llm = MetaResponsesProvider(
        settings, transport=httpx.MockTransport(handle))
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED
    assert len(requests) == 2 and len(registry.ledger.transactions) == 1
    assert len(run.llm_calls) == 3


@pytest.mark.asyncio
async def test_f3_budget_1515_context_free_semantic_payload():
    engine, registry, provider = _engine(
        [_transfer()], settings=Settings(
            max_retries=1, token_budget_per_workflow=1515))
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    sent = provider.payloads[-1]
    assert "evidence_spans" not in sent and "ledger_snapshot" not in sent


# F4: pruning keeps mandatory ledger rows (current ops and reads).

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


@pytest.mark.asyncio
async def test_f4_pruning_keeps_current_transfer_and_read_rows():
    engine, registry, provider = _engine(
        [_transfer()], settings=Settings(
            max_retries=1, token_budget_per_workflow=20000))
    for i in range(35):
        registry.ledger.transfer("acc_alice", "acc_bob", 1.0, f"history-{i}")
    requests = []
    engine.semantic._llm = _meta(requests)
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED and len(requests) == 1
    sent = json.loads(requests[0]["input"].split("\n", 1)[1])
    current = registry.ledger.find_by_key(run.intents[0].intent_id)
    assert any(t["tx_id"] == current.tx_id
               for t in sent["evidence"]["transactions"])

    engine2, registry2, provider2 = _engine(
        [{"tool_name": "list_transactions", "account_id": "acc_alice",
          "limit": 35}],
        settings=Settings(max_retries=1, token_budget_per_workflow=20000))
    for i in range(35):
        registry2.ledger.transfer("acc_alice", "acc_bob", 1.0, f"history-{i}")
    requests2 = []
    engine2.semantic._llm = _meta(requests2)
    await engine2.create_workflow(_goal(required=True))
    assert len(requests2) == 1
    sent2 = json.loads(requests2[0]["input"].split("\n", 1)[1])
    assert len(sent2["evidence"]["transactions"]) == 35
