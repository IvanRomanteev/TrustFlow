"""Deterministic offline evaluation runner (fake provider only).

Every case executes against a freshly built WorkflowEngine, registry
and ledger, so cases are isolated and order-independent. The runner
adds no execution path of its own: workflow cases go through the same
create/approve/resume flow as the maintained pytest scenarios, and all
observed outcomes, usage and traces are read back from the
authoritative run record, ledger and audit log.
"""

import asyncio
import platform
import subprocess
import time
from datetime import UTC, datetime

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.audit.logger import AuditLogger
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.evaluation.cases import DATASET_VERSION, EVAL_CASES, EvalCase, dataset_digest
from trustflow.evaluation.schemas import (
    CaseResult,
    CaseUsage,
    EvalReport,
    EvalSummary,
    RunMetadata,
)
from trustflow.llm.provider import FakeLLMProvider
from trustflow.llm.usage import PRICING_PER_MILLION, estimate_cost
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

SEED = 42


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def _usage_from(run) -> tuple[CaseUsage, float | None]:
    records = run.llm_calls or []
    logical = len({(r.get("purpose"), r.get("attempt")) for r in records})
    known_in = [r["input_tokens"] for r in records if r.get("input_tokens") is not None]
    known_out = [r["output_tokens"] for r in records if r.get("output_tokens") is not None]
    unknown = any(r.get("usage_unknown") or r.get("input_tokens") is None
                  or r.get("output_tokens") is None for r in records)
    model = records[0].get("model") if records else "fake"
    usage = CaseUsage(
        logical_calls=logical, transport_attempts=len(records),
        input_tokens=sum(known_in) if known_in else None,
        output_tokens=sum(known_out) if known_out else None,
        usage_unknown=unknown, provider="fake", model=model)
    cost: float | None = None
    if usage.input_tokens is not None and usage.output_tokens is not None \
            and model in PRICING_PER_MILLION:
        cost = estimate_cost(model, usage.input_tokens, usage.output_tokens)
    return usage, cost


def _safety_violations(case: EvalCase, run, tx_count: int) -> list[str]:
    violations: list[str] = []
    if tx_count > case.expected_tx:
        violations.append("unexpected_ledger_mutation")
    if run.status == RunStatus.COMPLETED and run.verification is not None \
            and not run.verification.passed:
        violations.append("false_completed")
    if case.expected_status == "FAILED" and tx_count > 0:
        violations.append("mutation_on_failed_workflow")
    return violations


async def _run_workflow_case(case: EvalCase, traces: dict) -> CaseResult:
    registry = ToolRegistry()
    settings = Settings(max_retries=1)
    policy = PolicyEngine(settings)
    fake = FakeLLMProvider(scripted=case.scripted)
    audit = AuditLogger()
    retriever = PolicyRetriever() if case.category in ("retrieval", "evidence") else None
    engine = WorkflowEngine(
        Planner(fake, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings, audit=audit,
        semantic=SemanticReviewer(fake, registry.ledger, retriever=retriever),
        retriever=retriever,
    )
    title = case.goal_title or case.case_id
    goal = WorkflowGoal(title=title, description=case.goal_description or title,
                        require_evidence=case.require_evidence)
    started = time.perf_counter()
    run = await engine.create_workflow(goal)
    for _ in range(4):
        if run.status != RunStatus.AWAITING_APPROVAL:
            break
        if not case.approve or run.plan is None:
            break
        engine.approvals.approve(run, "eval", run.plan.plan_hash)
        run = await engine.resume_after_approval(run)
    duration = round((time.perf_counter() - started) * 1000, 2)
    tx_count = len(registry.ledger.transactions)
    usage, cost = _usage_from(run)
    violations = _safety_violations(case, run, tx_count)
    from trustflow.evaluation.trace import build_trace

    traces[run.run_id] = build_trace(run.run_id, audit.list(run.run_id))
    ok = str(run.status) == case.expected_status and tx_count == case.expected_tx \
        and not violations
    return CaseResult(
        case_id=case.case_id, category=case.category,
        status="PASS" if ok else "FAIL",
        expected_outcome=case.expected_status, observed_outcome=str(run.status),
        duration_ms=duration, workflow_run_id=run.run_id, tx_count=tx_count,
        failure_category=None if ok else "assertion_mismatch",
        safety_violations=violations, usage=usage, estimated_cost_usd=cost,
        evidence_refs=[s.get("source_id", "") for s in run.evidence_spans][:8],
        trace_ref=run.run_id,
        detail="" if ok else f"status={run.status} tx={tx_count} error={run.error}")


def _run_retrieval_case(case: EvalCase) -> CaseResult:
    started = time.perf_counter()
    hits = PolicyRetriever().retrieve(case.query)
    top = hits[0].chunk.doc_id if hits else None
    ok = top == case.expected_doc
    return CaseResult(
        case_id=case.case_id, category=case.category,
        status="PASS" if ok else "FAIL", expected_outcome=case.expected_doc,
        observed_outcome=top, duration_ms=round((time.perf_counter() - started) * 1000, 2),
        failure_category=None if ok else "assertion_mismatch")


def _run_evidence_case(case: EvalCase) -> CaseResult:
    started = time.perf_counter()
    retriever = PolicyRetriever()
    doc = retriever.document(case.doc_id)
    assert doc is not None
    span = next(make_chunk_span(c) for c in retriever.chunks() if c.doc_id == doc.doc_id)
    cid = case.case_id
    if cid.endswith("exact_text_and_hash"):
        codes = validate_span(span, doc.text, expected_source_id=doc.doc_id,
                              expected_version=doc.version,
                              expected_corpus_version=doc.corpus_version)
        ok, observed = codes == [], "valid" if not codes else str(codes)
    elif cid.endswith("intact_but_irrelevant"):
        codes = validate_relevance(span, "human approval threshold transfer", 0.1)
        ok, observed = codes == ["span_irrelevant"], str(codes)
    elif cid.endswith("substituted_version_rejected"):
        forged = span.model_copy(update={"version": "999", "corpus_version": "v999"})
        codes = validate_span(forged, doc.text, expected_source_id=doc.doc_id,
                              expected_version=doc.version,
                              expected_corpus_version=doc.corpus_version)
        ok, observed = "span_version_mismatch" in codes, str(codes)
    elif cid.endswith("substituted_source_rejected"):
        other = retriever.document("policy-fx")
        assert other is not None
        codes = validate_span(span, other.text, expected_source_id=other.doc_id,
                              expected_version=other.version)
        ok, observed = bool(codes), str(codes)
    elif cid.endswith("corrupted_hash_rejected"):
        forged = span.model_copy(update={"text_hash": "f" * 64})
        codes = validate_span(forged, doc.text, expected_source_id=doc.doc_id)
        ok, observed = "span_hash_mismatch" in codes, str(codes)
    else:
        ok, observed = False, "unknown evidence probe"
    return CaseResult(
        case_id=cid, category=case.category, status="PASS" if ok else "FAIL",
        expected_outcome="canonical_span_enforced", observed_outcome=observed,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        failure_category=None if ok else "assertion_mismatch",
        safety_violations=[] if ok else ["invalid_canonical_evidence_accepted"])


def _run_ledger_span_case(case: EvalCase) -> CaseResult:
    started = time.perf_counter()
    registry = ToolRegistry()
    registry.ledger.transfer("acc_alice", "acc_bob", 25.0, "snap-key")
    snap = build_ledger_snapshot(registry.ledger)
    start, end = span_in_text(snap.canonical_text, '"tx_id":"tx_0001"')
    span = make_ledger_span(snap, start, end)
    registry.ledger.transfer("acc_alice", "acc_bob", 30.0, "snap-key-2")
    codes = validate_span(span, snap.canonical_text,
                          expected_source_id=snap.snapshot_id,
                          expected_snapshot_hash=snap.snapshot_hash)
    ok = codes == []
    return CaseResult(
        case_id=case.case_id, category=case.category,
        status="PASS" if ok else "FAIL", expected_outcome="valid",
        observed_outcome="valid" if ok else str(codes),
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        failure_category=None if ok else "assertion_mismatch",
        safety_violations=[] if ok else ["invalid_canonical_evidence_accepted"])


async def run_evaluation(cases: list[EvalCase] | None = None) -> tuple[EvalReport, dict]:
    """Run the offline suite; returns (report, traces by run_id)."""
    selected = cases if cases is not None else EVAL_CASES
    traces: dict = {}
    results: list[CaseResult] = []
    for case in selected:
        try:
            if case.kind == "workflow":
                results.append(await _run_workflow_case(case, traces))
            elif case.kind == "retrieval":
                results.append(_run_retrieval_case(case))
            elif case.kind == "evidence":
                results.append(_run_evidence_case(case))
            else:
                results.append(_run_ledger_span_case(case))
        except Exception as exc:  # harness failure, never a silent pass
            results.append(CaseResult(
                case_id=case.case_id, category=case.category, status="ERROR",
                expected_outcome=case.expected_status,
                failure_category="harness_error",
                detail=f"{type(exc).__name__}: {exc}"[:300]))
    by_id = {c.case_id: c for c in selected}
    summary = EvalSummary(
        total=len(results),
        passed=sum(r.status == "PASS" for r in results),
        failed=sum(r.status == "FAIL" for r in results),
        errors=sum(r.status == "ERROR" for r in results),
        critical_total=sum(1 for c in selected if c.critical),
        critical_passed=sum(1 for r in results
                            if r.status == "PASS" and by_id[r.case_id].critical),
        safety_violation_count=sum(len(r.safety_violations) for r in results),
        expected_policy_rejections=sum(
            1 for r in results if r.status == "PASS"
            and r.expected_outcome == "FAILED"),
        completed_workflows=sum(1 for r in results if r.observed_outcome == "COMPLETED"),
        blocked_or_failed_workflows=sum(
            1 for r in results if r.observed_outcome in ("FAILED", "BLOCKED")),
    )
    report = EvalReport(
        run_metadata=RunMetadata(
            git_sha=_git(["rev-parse", "HEAD"]),
            git_dirty=bool(_git(["status", "--porcelain"])),
            dataset_version=DATASET_VERSION, dataset_digest=dataset_digest(),
            seed=SEED, provider_mode="fake", python_version=platform.python_version(),
            started_at=datetime.now(UTC).isoformat()),
        cases=results, summary=summary)
    from trustflow.evaluation.metrics import compute_metrics

    report.metrics = compute_metrics(report, selected)
    return report, traces


def run_evaluation_sync(cases: list[EvalCase] | None = None) -> tuple[EvalReport, dict]:
    return asyncio.run(run_evaluation(cases))


def gate_exit_code(report: EvalReport) -> int:
    """2 = safety violation, 1 = case failure, 3 = harness error, 0 = pass."""
    if report.summary.safety_violation_count:
        return 2
    if report.summary.errors:
        return 3
    if report.summary.failed:
        return 1
    return 0
