"""Residual M2 remediation regressions: R2/R3/R5/R6 re-review findings.

All cases assert absence of side effects or bounded verification-only
recovery, never merely the presence of an error.
"""

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
from trustflow.llm.usage import UsageTracker
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.retrieval.models import (
    build_ledger_snapshot,
    make_ledger_span,
    span_in_text,
)
from trustflow.tools.ledger import SyntheticLedger
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
            from trustflow.core.errors import LLMResponseError
            raise LLMResponseError("injected outage", record=rec)
        return raw, rec


def _engine(scripted, settings=None, provider=None, retriever=None):
    registry = ToolRegistry()
    settings = settings or Settings(max_retries=1)
    policy = PolicyEngine(settings)
    provider = provider or CountingProvider(scripted)
    retriever = retriever if retriever is not None else PolicyRetriever()
    engine = WorkflowEngine(
        Planner(provider, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(provider, registry.ledger, retriever=retriever),
        retriever=retriever,
    )
    return engine, registry, provider


def _goal(required=True):
    return WorkflowGoal(title="transfer approval verification",
                        description="transfer approval verification evidence",
                        require_evidence=required)


def _after_commit(engine, registry, action):
    execute = engine.executor.execute

    async def wrapped(run, plan):
        results = await execute(run, plan)
        assert len(registry.ledger.transactions) >= 1
        action(run)
        return results

    engine.executor.execute = wrapped


# F1/R2: deleting policy spans after commit must not complete.

@pytest.mark.asyncio
async def test_missing_policy_spans_after_commit_never_complete():
    engine, registry, provider = _engine([_transfer()])
    _after_commit(engine, registry, lambda run: setattr(run, "evidence_spans", []))
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1
    assert provider.reviews == 0
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.VERIFICATION_PENDING
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.FAILED
    assert len(registry.ledger.transactions) == 1


@pytest.mark.asyncio
async def test_partial_policy_deletion_after_commit_never_complete():
    engine, registry, provider = _engine([_transfer()])

    def drop_one(run):
        run.evidence_spans = [
            s for s in run.evidence_spans if s["source_type"] != "policy"
        ] or run.evidence_spans[1:]

    _after_commit(engine, registry, drop_one)
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1
    assert provider.reviews == 0


# N1/R2: malformed snapshot amount is typed recovery, never VERIFYING stall.

@pytest.mark.parametrize("value", ["invalid", None, 10**400])
@pytest.mark.asyncio
async def test_malformed_snapshot_amount_typed_recovery_and_healthy_retry(value):
    engine, registry, provider = _engine([_transfer()])
    original = engine.semantic.ledger_snapshot

    def malformed(plan):
        snapshot = original(plan)
        snapshot.transactions[0]["amount"] = value
        return snapshot

    engine.semantic.ledger_snapshot = malformed
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1
    assert provider.reviews == 0
    assert run.llm_calls and run.verification is not None
    engine.semantic.ledger_snapshot = original
    run.ledger_snapshot = None
    run = await engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1


# F2/R3: canonical text is authoritative; a replaced duplicate index
# must not validate a foreign cited transaction.

def test_forged_duplicate_index_does_not_validate_foreign_canonical():
    engine, registry, provider = _engine([_transfer()])
    ledger = registry.ledger
    ledger.transfer("acc_alice", "acc_bob", 50.0, "op-current")
    reviewer = engine.semantic
    from trustflow.domain.models import Plan, PlanStep
    plan = Plan(goal_title="t", steps=[PlanStep(
        step_id="s1", tool_name="simulate_transfer", operation_id="op-current",
        intent_id="op-current",
        args={"from_account": "acc_alice", "to_account": "acc_bob",
              "amount": 50.0, "currency": "USD"})])
    foreign = SyntheticLedger()
    foreign.transfer("acc_ops", "acc_bob", 99.0, "foreign-operation")
    foreign_snapshot = build_ledger_snapshot(foreign)
    current = reviewer.ledger_snapshot(plan)
    forged = foreign_snapshot.model_copy(
        update={"transactions": [dict(t) for t in current.transactions],
                "balances": dict(current.balances)})
    start, end = span_in_text(forged.canonical_text, '"tx_id":"tx_0001"')
    span = make_ledger_span(forged, start, end)
    codes = reviewer.validate_evidence_spans(
        [span], WorkflowGoal(title="transfer approval verification"), plan, forged)
    assert codes


# F3/R5 + N2/R6: optional planner context shrinks to the budget and
# untransmitted legacy context is never reserved.

class RecordingTracker(UsageTracker):
    def __init__(self, budget=100000):
        super().__init__(budget, workflow_id="probe")
        self.reservations = []

    def reserve(self, estimate):
        self.reservations.append(estimate)
        super().reserve(estimate)


def test_legacy_reserves_only_transmitted_input():
    provider = CountingProvider([_transfer()])

    class Legacy:
        def propose_plan(self, title, description, repair_feedback=None):
            return provider.propose_plan(title, description, repair_feedback)

    planner = Planner(Legacy())
    a, b = RecordingTracker(), RecordingTracker()
    planner.create_plan(WorkflowGoal(title="transfer"), tracker=a, retrieval_context="")
    planner.create_plan(WorkflowGoal(title="transfer"), tracker=b,
                        retrieval_context="x" * 2000)
    assert a.reservations == b.reservations


@pytest.mark.asyncio
async def test_large_optional_planner_context_keeps_budget_1800_success():
    from trustflow.retrieval.models import PolicyDocument
    doc = PolicyDocument(
        doc_id="large", version="1", corpus_version="synthetic-policies-v1",
        title="transfer", text="transfer approval verification " * 80)
    retriever = PolicyRetriever(corpus=[doc])
    engine, registry, _ = _engine(
        [_transfer()], settings=Settings(
            max_retries=1, token_budget_per_workflow=1800), retriever=retriever)
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    assert len(run.llm_calls) == 2


# F4/R5: mandatory semantic evidence is never silently truncated by
# the provider; the transmitted prompt is whole structured JSON data.

@pytest.mark.asyncio
async def test_mandatory_semantic_payload_not_silently_truncated():
    engine, registry, provider = _engine([_transfer(1.0)])
    for index in range(35):
        registry.ledger.transfer("acc_alice", "acc_bob", 1.0,
                                 f"historical-{index}")
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "output_text": json.dumps({
                "verdict": "PASS", "confidence": 1, "issues": [],
                "summary": "offline response"}),
            "usage": {"input_tokens": 7, "output_tokens": 11}})

    settings = Settings(llm_mode="fake", model_api_key="offline-placeholder",
                        llm_base_url="https://offline.invalid", llm_max_retries=0)
    engine.semantic._llm = MetaResponsesProvider(
        settings, transport=httpx.MockTransport(handle))
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.COMPLETED
    assert len(requests) == 1
    prompt = requests[0]["input"]
    assert '"evidence_spans"' in prompt and '"ledger_snapshot"' in prompt
    assert run.ledger_snapshot["snapshot_hash"] in prompt
    # The JSON payload embedded in the prompt parses as a whole.
    payload_text = prompt.split("\n", 1)[1] if "\n" in prompt else prompt
    assert json.loads(payload_text.rsplit("Return the review JSON.", 1)[0]
                      if False else _extract_payload(prompt))


def _extract_payload(prompt: str) -> str:
    start = prompt.index("{")
    return prompt[start:]


def test_semantic_payload_unicode_and_oversize_mandatory_is_controlled():
    import pytest as _pytest

    from trustflow.core.errors import LLMResponseError

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "output_text": json.dumps({"verdict": "PASS", "confidence": 1,
                                       "issues": [], "summary": "ok"}),
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    settings = Settings(llm_mode="fake", model_api_key="offline-placeholder",
                        llm_base_url="https://offline.invalid", llm_max_retries=0)
    provider = MetaResponsesProvider(settings, transport=httpx.MockTransport(handle))
    unicode_payload = {
        "goal": {"title": "проверка 😀", "description": "é"},
        "plan_steps": [],
        "evidence": {"balances": {}, "transactions": []},
        "evidence_spans": [{"quoted_text": '"tx_id":"tx_0001"'}],
        "ledger_snapshot": {"snapshot_id": "ledger-x", "snapshot_hash": "h"},
    }
    provider.semantic_review(unicode_payload)
    prompt = json.loads(requests[0].content)["input"]
    parsed = json.loads(_extract_payload(prompt))
    assert parsed["goal"]["title"] == "проверка 😀"
    # Mandatory evidence that cannot fit even after dropping uncited
    # transactions: controlled typed error, zero further HTTP calls.
    huge = {**unicode_payload,
            "evidence_spans": [{"quoted_text": "я" * 7000,
                                "text_hash": "x" * 64}]}
    with _pytest.raises(LLMResponseError):
        provider.semantic_review(huge)
    assert len(requests) == 1
