"""M2 eval scenarios: policy-retrieval and evidence-span families.

Synthetic in-repository scope agreed for M2 (docs/m2.md), not a
reproduction of any external Playbook. Adds 11 scenarios to the 22
M1 scenarios in test_scenarios.py (33 total, above the agreed 30).
All offline, fake provider only.
"""

import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.retrieval.models import (
    build_ledger_snapshot,
    make_chunk_span,
    make_ledger_span,
    span_in_text,
    validate_relevance,
    validate_span,
)
from trustflow.tools.registry import ToolRegistry


def _transfer(amount=100.0):
    return {"tool_name": "simulate_transfer", "from_account": "acc_alice",
            "to_account": "acc_bob", "amount": amount, "currency": "USD"}


def _engine(scripted, retriever=None):
    registry = ToolRegistry()
    settings = Settings(max_retries=1)
    policy = PolicyEngine(settings)
    fake = FakeLLMProvider(scripted=scripted)
    retriever = retriever if retriever is not None else PolicyRetriever()
    engine = WorkflowEngine(
        Planner(fake, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(fake, registry.ledger, retriever=retriever),
        retriever=retriever,
    )
    return engine, registry


# --- policy-retrieval family -------------------------------------------------

def test_policy_retrieval_approval_threshold():
    hits = PolicyRetriever().retrieve(
        "transfer above 1000 USD requires human approval")
    assert hits and hits[0].chunk.doc_id == "policy-approval"
    assert hits[0].score > 0


def test_policy_retrieval_accounts():
    hits = PolicyRetriever().retrieve(
        "known synthetic accounts self transfer prohibited")
    assert hits and hits[0].chunk.doc_id == "policy-accounts"


def test_policy_retrieval_fx():
    hits = PolicyRetriever().retrieve("supported synthetic FX pairs USD EUR")
    assert hits and hits[0].chunk.doc_id == "policy-fx"


@pytest.mark.asyncio
async def test_retrieved_policy_attached_but_deterministic_gate_decides():
    engine, registry = _engine([_transfer(1500.0)])
    run = await engine.create_workflow(WorkflowGoal(
        title="transfer 1500 approval", description="transfer 1500 USD approval"))
    assert run.retrieval and run.evidence_spans
    assert run.ledger_snapshot is None  # no execution yet
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert registry.ledger.transactions == []
    engine.approvals.approve(run, "eval", run.plan.plan_hash)
    run = await engine.resume_after_approval(run)
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    assert run.ledger_snapshot is not None
    assert any(s["source_type"] == "ledger" for s in run.evidence_spans)


# --- evidence-span family ----------------------------------------------------

def test_evidence_span_exact_text_and_hash():
    retriever = PolicyRetriever()
    doc = retriever.document("policy-verification")
    assert doc is not None
    span = next(make_chunk_span(c) for c in retriever.chunks()
                if c.doc_id == doc.doc_id)
    assert doc.text[span.start:span.end] == span.quoted_text
    assert validate_span(span, doc.text, expected_source_id=doc.doc_id,
                         expected_version=doc.version,
                         expected_corpus_version=doc.corpus_version) == []


def test_evidence_span_intact_but_irrelevant():
    retriever = PolicyRetriever()
    doc = retriever.document("policy-fx")
    assert doc is not None
    span = next(make_chunk_span(c) for c in retriever.chunks()
                if c.doc_id == doc.doc_id)
    assert validate_span(span, doc.text, expected_source_id=doc.doc_id) == []
    assert validate_relevance(
        span, "human approval threshold transfer", 0.1) == ["span_irrelevant"]


def test_evidence_span_substituted_version_rejected():
    retriever = PolicyRetriever()
    doc = retriever.document("policy-approval")
    assert doc is not None
    span = next(make_chunk_span(c) for c in retriever.chunks()
                if c.doc_id == doc.doc_id)
    forged = span.model_copy(update={"version": "999", "corpus_version": "v999"})
    codes = validate_span(forged, doc.text, expected_source_id=doc.doc_id,
                          expected_version=doc.version,
                          expected_corpus_version=doc.corpus_version)
    assert "span_version_mismatch" in codes


def test_evidence_span_substituted_source_rejected():
    retriever = PolicyRetriever()
    doc = retriever.document("policy-approval")
    other = retriever.document("policy-fx")
    assert doc is not None and other is not None
    span = next(make_chunk_span(c) for c in retriever.chunks()
                if c.doc_id == doc.doc_id)
    codes = validate_span(span, other.text, expected_source_id=other.doc_id,
                          expected_version=other.version)
    assert codes  # source/text no longer match the cited document


def test_evidence_span_corrupted_hash_rejected():
    retriever = PolicyRetriever()
    doc = retriever.document("policy-limits")
    assert doc is not None
    span = next(make_chunk_span(c) for c in retriever.chunks()
                if c.doc_id == doc.doc_id)
    forged = span.model_copy(update={"text_hash": "f" * 64})
    assert "span_hash_mismatch" in validate_span(
        forged, doc.text, expected_source_id=doc.doc_id)


def test_ledger_span_cites_stable_snapshot():
    registry = ToolRegistry()
    registry.ledger.transfer("acc_alice", "acc_bob", 25.0, "snap-key")
    snap = build_ledger_snapshot(registry.ledger)
    start, end = span_in_text(snap.canonical_text, '"tx_id":"tx_0001"')
    span = make_ledger_span(snap, start, end)
    registry.ledger.transfer("acc_alice", "acc_bob", 30.0, "snap-key-2")
    assert validate_span(span, snap.canonical_text,
                         expected_source_id=snap.snapshot_id,
                         expected_snapshot_hash=snap.snapshot_hash) == []


@pytest.mark.asyncio
async def test_mandatory_evidence_workflow_completes_with_citations():
    engine, registry = _engine([_transfer(50.0)])
    run = await engine.create_workflow(WorkflowGoal(
        title="transfer approval verification",
        description="transfer with approval and verification evidence",
        require_evidence=True))
    assert run.status == RunStatus.COMPLETED
    assert len(registry.ledger.transactions) == 1
    assert any(s["source_type"] == "policy" for s in run.evidence_spans)
    assert any(s["source_type"] == "ledger" for s in run.evidence_spans)
