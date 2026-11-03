"""M2 remediation regressions R1-R8 (independent review of f0a1536).

Each test asserts absence of side effects or verification-only
recovery, not merely the presence of an error.
"""

import pytest
from pydantic import ValidationError

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.errors import LLMResponseError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider
from trustflow.llm.usage import UsageTracker
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.retrieval.models import (
    EvidenceSpan,
    build_ledger_snapshot,
    make_chunk_span,
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


def _goal(required=True, title="transfer approval verification"):
    return WorkflowGoal(title=title,
                        description="transfer approval verification evidence",
                        require_evidence=required)


def _after_commit(engine, registry, action):
    execute = engine.executor.execute

    async def wrapped(run, plan):
        results = await execute(run, plan)
        assert len(registry.ledger.transactions) == 1
        action(run)
        return results

    engine.executor.execute = wrapped


# R1: invalid mandatory evidence at retrieval / while awaiting approval.

@pytest.mark.parametrize("updates", [
    {"text_hash": "0" * 64}, {"source_id": "not-in-corpus"}, {"version": "999"},
])
@pytest.mark.asyncio
async def test_r1_invalid_at_retrieval_prevents_payment(updates):
    engine, registry, provider = _engine([_transfer()])
    retrieve = engine.retriever.retrieve

    def corrupted(query):
        return [row.model_copy(update={
            "span": row.span.model_copy(update=updates)})
            for row in retrieve(query)]

    engine.retriever.retrieve = corrupted
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.FAILED
    assert registry.ledger.transactions == []
    assert provider.reviews == 0


@pytest.mark.asyncio
async def test_r1_corrupt_while_awaiting_approval_prevents_payment():
    engine, registry, _ = _engine([_transfer(1500.0)])
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.AWAITING_APPROVAL
    run.evidence_spans[0]["text_hash"] = "0" * 64
    engine.approvals.approve(run, "eval", run.plan.plan_hash)
    run = await engine.resume_after_approval(run)
    assert run.status == RunStatus.FAILED
    assert registry.ledger.transactions == []


# R2: malformed mandatory evidence after commit never completes.

@pytest.mark.parametrize("mode", ["bad_type", "missing_field",
                                  "snapshot_failure", "spans_failure"])
@pytest.mark.asyncio
async def test_r2_post_commit_malformed_never_completes(mode):
    engine, registry, provider = _engine([_transfer()])

    def corrupt(run):
        if mode == "bad_type":
            run.evidence_spans[0]["start"] = "invalid"
        elif mode == "missing_field":
            del run.evidence_spans[0]["text_hash"]
        else:
            def broken(*args):
                raise ValueError("injected construction failure")

            attr = "ledger_snapshot" if mode == "snapshot_failure" else "ledger_spans"
            setattr(engine.semantic, attr, broken)

    _after_commit(engine, registry, corrupt)
    run = await engine.create_workflow(_goal())
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert len(registry.ledger.transactions) == 1
    assert run.verification is not None and run.verification.passed
    assert provider.reviews == 0


# R3: ledger citations bind to this workflow's operation.

@pytest.mark.asyncio
async def test_r3_second_workflow_cites_its_own_transaction():
    engine, registry, _ = _engine([_transfer()])
    first = await engine.create_workflow(_goal())
    second = await engine.create_workflow(_goal())
    assert first.status == second.status == RunStatus.COMPLETED
    tx = registry.ledger.find_by_key(second.intents[0].intent_id)
    spans = [s for s in second.evidence_spans if s["source_type"] == "ledger"]
    assert tx is not None
    assert any(tx.tx_id in s["quoted_text"] for s in spans)


def test_r3_foreign_and_irrelevant_ledger_spans_rejected():
    ledger = SyntheticLedger()
    ledger.transfer("acc_alice", "acc_bob", 50.0, "op-current")
    reviewer = SemanticReviewer(FakeLLMProvider(), ledger,
                                retriever=PolicyRetriever())
    plan_goal = WorkflowGoal(title="transfer approval verification")
    from trustflow.domain.models import Plan, PlanStep
    plan = Plan(goal_title="t", steps=[PlanStep(
        step_id="s1", tool_name="simulate_transfer", operation_id="op-current",
        intent_id="op-current",
        args={"from_account": "acc_alice", "to_account": "acc_bob",
              "amount": 50.0, "currency": "USD"})])
    snapshot = reviewer.ledger_snapshot(plan)
    start, end = span_in_text(snapshot.canonical_text, "synthetic")
    irrelevant = make_ledger_span(snapshot, start, end)
    assert "span_irrelevant" in reviewer.validate_evidence_spans(
        [irrelevant], plan_goal, plan, snapshot)
    foreign_ledger = SyntheticLedger()
    foreign_ledger.transfer("acc_ops", "acc_bob", 99.0, "foreign-operation")
    foreign = build_ledger_snapshot(foreign_ledger)
    fs, fe = span_in_text(foreign.canonical_text, '"tx_id":"tx_0001"')
    foreign_span = make_ledger_span(foreign, fs, fe)
    assert reviewer.validate_evidence_spans(
        [foreign_span], plan_goal, plan, foreign)


# R4: another workflow must not break healthy recovery.

@pytest.mark.asyncio
async def test_r4_other_workflow_does_not_invalidate_recovery():
    engine, registry, provider = _engine([_transfer()])
    provider.outage = True
    run_a = await engine.create_workflow(_goal())
    assert run_a.status == RunStatus.VERIFICATION_PENDING
    snapshot_a = run_a.ledger_snapshot["snapshot_id"]
    provider.outage = False
    run_b = await engine.create_workflow(_goal())
    assert run_b.status == RunStatus.COMPLETED
    run_a = await engine.retry_verification(run_a)
    assert run_a.status == RunStatus.COMPLETED
    assert run_a.ledger_snapshot["snapshot_id"] == snapshot_a
    assert len(registry.ledger.transactions) == 2


# R5: advisory retrieval must not change a previously successful outcome.

@pytest.mark.asyncio
async def test_r5_advisory_budget_keeps_completed_outcome():
    engine, registry, _ = _engine(
        [_transfer()], settings=Settings(
            max_retries=1, token_budget_per_workflow=1800))
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    assert len(run.llm_calls) == 2


# R6: reservation includes the actually transmitted bounded context.

def test_r6_reservation_includes_transmitted_context():
    observed = []

    class Aware:
        supports_attempt_tracker = True
        supports_retrieval_context = True

        def propose_plan(self, title, description, repair_feedback=None,
                         tracker=None, retry_reserve_tokens=0,
                         retrieval_context=None):
            observed.append((retry_reserve_tokens, len(retrieval_context or "")))
            return FakeLLMProvider(scripted=[_transfer()]).propose_plan(
                title, description, repair_feedback)

    planner = Planner(Aware(), PolicyEngine(Settings()))
    planner.create_plan(WorkflowGoal(title="transfer"),
                        tracker=UsageTracker(100000), retrieval_context="")
    planner.create_plan(WorkflowGoal(title="transfer"),
                        tracker=UsageTracker(100000),
                        retrieval_context="x" * 2000)
    assert observed[1][1] == 2000
    assert observed[1][0] >= observed[0][0] + 500


# R7: chunk_id must resolve and contain the span.

def test_r7_forged_and_out_of_chunk_spans_rejected():
    retriever = PolicyRetriever()
    reviewer = SemanticReviewer(FakeLLMProvider(), SyntheticLedger(),
                                retriever=retriever)
    doc = retriever.document("policy-approval")
    goal = WorkflowGoal(title="transfer approval verification",
                        description="transfer approval")
    from trustflow.domain.models import Plan
    plan = Plan(goal_title="t", steps=[])
    span = next(make_chunk_span(c) for c in retriever.chunks()
                if c.doc_id == doc.doc_id)
    forged = span.model_copy(update={"chunk_id": "policy-fx#999"})
    assert reviewer.validate_evidence_spans([forged], goal, plan)
    chunk = next(c for c in retriever.chunks() if c.chunk_id == span.chunk_id)
    outside = span.model_copy(update={
        "start": chunk.end, "end": chunk.end + 5,
        "quoted_text": doc.text[chunk.end:chunk.end + 5]})
    from trustflow.retrieval.models import sha256_text
    outside = outside.model_copy(
        update={"text_hash": sha256_text(outside.quoted_text)})
    assert reviewer.validate_evidence_spans([outside], goal, plan)


# R8: legacy providers without the new keyword stay compatible.

@pytest.mark.asyncio
async def test_r8_legacy_fake_subclass_and_plain_legacy_provider():
    class LegacyFake(FakeLLMProvider):
        def propose_plan(self, title, description, repair_feedback=None):
            return super().propose_plan(title, description, repair_feedback)

    engine, registry, _ = _engine(
        [_transfer()], provider=LegacyFake(scripted=[_transfer()]))
    run = await engine.create_workflow(_goal(required=False))
    assert run.status == RunStatus.COMPLETED

    class Legacy:
        def propose_plan(self, title, description, repair_feedback=None):
            return FakeLLMProvider(scripted=[_transfer()]).propose_plan(
                title, description, repair_feedback)

        def semantic_review(self, payload):
            return FakeLLMProvider().semantic_review(payload)

    registry2 = ToolRegistry()
    settings = Settings(max_retries=1)
    policy = PolicyEngine(settings)
    legacy = Legacy()
    engine2 = WorkflowEngine(
        Planner(legacy, policy), Executor(registry2, settings),
        Verifier(registry2.ledger, registry2.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(legacy, registry2.ledger),
        retriever=PolicyRetriever())
    run2 = await engine2.create_workflow(_goal(required=False))
    assert run2.status == RunStatus.COMPLETED


# P3 clarification: strict integer offsets (integer-valued 0.0 is JSON-valid).

@pytest.mark.parametrize("value", [False, "0", 0.5])
def test_offsets_reject_bool_string_and_fraction(value):
    retriever = PolicyRetriever()
    data = make_chunk_span(retriever.chunks()[0]).model_dump()
    data["start"] = value
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate(data)


def test_offsets_accept_integer_valued_json_number():
    retriever = PolicyRetriever()
    data = make_chunk_span(retriever.chunks()[0]).model_dump()
    data["start"] = 0.0
    assert EvidenceSpan.model_validate(data).start == 0
