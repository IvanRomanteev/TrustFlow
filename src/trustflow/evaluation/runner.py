"""Deterministic offline evaluation runner (fake provider only).

Every case executes against a freshly built WorkflowEngine, registry
and ledger, so cases are isolated and order-independent. The runner
executes the same assertions as the maintained pytest evaluation
suite (retrieval score, pre-approval lifecycle, post-approval snapshot
and citations, mandatory policy+ledger evidence, exact span text) and
validates terminal states with the authoritative safety detector in
``trustflow.evaluation.safety``. Outcomes, usage and traces are read
back from the authoritative run record, ledger and audit log.

Effective configuration is pinned to explicit fixture values (F6):
ambient TRUSTFLOW_* environment values cannot silently change the
offline safety path. The allowlisted effective configuration and its
digest are published in every report manifest.
"""

import asyncio
import hashlib
import json
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
from trustflow.evaluation.cases import (
    DATASET_VERSION,
    EVAL_CASES,
    EvalCase,
    assertion_contract_digest,
    dataset_digest,
    validate_cases,
)
from trustflow.evaluation.errors import safe_error_detail
from trustflow.evaluation.safety import safety_violations
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

# Pinned offline evaluation configuration (F6). Explicit constructor
# values override ambient environment in pydantic-settings, so the
# safety path under evaluation cannot drift with the host shell.
PINNED_SETTINGS: dict = {
    "llm_mode": "fake",
    "high_risk_threshold": 1000.0,
    "max_retries": 1,
    "max_steps": 20,
    "semantic_max_attempts": 3,
    "retrieval_top_k": 3,
    "retrieval_min_score": 0.1,
    "token_budget_per_workflow": 20000,
}


def pinned_settings() -> Settings:
    return Settings(**PINNED_SETTINGS)


def effective_config(cases: list[EvalCase]) -> dict:
    """Allowlisted, secret-free configuration actually in effect."""
    return {
        "provider_mode": "fake",
        "seed": SEED,
        "settings": dict(PINNED_SETTINGS),
        "dataset_version": DATASET_VERSION,
        "case_ids": sorted(c.case_id for c in cases),
        "pricing_models": sorted(PRICING_PER_MILLION),
    }


def config_digest(cases: list[EvalCase]) -> str:
    payload = json.dumps(
        {"config": effective_config(cases),
         "dataset_digest": dataset_digest(cases),
         "assertion_contract_digest": assertion_contract_digest()},
        sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def _usage_from(run) -> tuple[CaseUsage, float | None]:
    """Per-record usage/cost accounting (F3, F8).

    Cost is computed per usage record with that record's own model.
    The total is None whenever any record is incompletely tokenized or
    unpriced; the sum over fully priced records is exposed separately
    as ``partial_known_cost_usd`` and never presented as the total.
    Measured-zero tokens stay zero; only a record set with no known
    values at all yields None.
    """
    records = list(run.llm_calls or [])
    logical = len({(r.get("purpose"), r.get("attempt")) for r in records})
    known_in = [r["input_tokens"] for r in records if r.get("input_tokens") is not None]
    known_out = [r["output_tokens"] for r in records
                 if r.get("output_tokens") is not None]
    models = sorted({str(r.get("model")) for r in records if r.get("model")})
    unknown_records = 0
    unpriced_records = 0
    partial = 0.0
    fully_priced = 0
    for record in records:
        tokens_known = record.get("input_tokens") is not None \
            and record.get("output_tokens") is not None
        model = record.get("model")
        priced = model in PRICING_PER_MILLION
        if not tokens_known:
            unknown_records += 1
        if not priced:
            unpriced_records += 1
        if tokens_known and priced:
            partial += estimate_cost(model, record["input_tokens"],
                                     record["output_tokens"])
            fully_priced += 1
    complete = bool(records) and unknown_records == 0 and unpriced_records == 0
    usage = CaseUsage(
        logical_calls=logical, transport_attempts=len(records),
        input_tokens=sum(known_in) if known_in else None,
        output_tokens=sum(known_out) if known_out else None,
        usage_unknown=unknown_records > 0, provider="fake",
        model=models[0] if len(models) == 1 else None, models=models,
        partial_known_cost_usd=round(partial, 8) if fully_priced else None,
        unpriced_records=unpriced_records,
        unknown_token_records=unknown_records)
    return usage, (round(partial, 8) if complete else None)


async def _run_workflow_case(case: EvalCase, traces: dict) -> CaseResult:
    registry = ToolRegistry()
    settings = pinned_settings()
    policy = PolicyEngine(settings)
    fake = FakeLLMProvider(scripted=case.scripted)
    audit = AuditLogger()
    needs_retrieval = case.category in ("retrieval", "evidence")
    retriever = PolicyRetriever() if needs_retrieval else None
    initial_total = registry.ledger.total_balance()
    engine = WorkflowEngine(
        Planner(fake, policy), Executor(registry, settings),
        Verifier(registry.ledger, initial_total),
        policy=policy, settings=settings, audit=audit,
        semantic=SemanticReviewer(fake, registry.ledger, retriever=retriever),
        retriever=retriever,
    )
    title = case.goal_title or case.case_id
    goal = WorkflowGoal(title=title, description=case.goal_description or title,
                        require_evidence=case.require_evidence)
    started = time.perf_counter()
    run = await engine.create_workflow(goal)
    # Pre-approval lifecycle assertions (maintained M2 semantics): a
    # case that requires approval must pause with an empty ledger and
    # no ledger snapshot before any approval is granted.
    lifecycle_failures: list[str] = []
    if case.approve:
        if run.status != RunStatus.AWAITING_APPROVAL:
            lifecycle_failures.append("approval_pause_missing")
        if registry.ledger.transactions:
            lifecycle_failures.append("ledger_mutated_before_approval")
        if run.ledger_snapshot is not None:
            lifecycle_failures.append("ledger_snapshot_before_approval")
        if needs_retrieval and not (run.retrieval and run.evidence_spans):
            lifecycle_failures.append("retrieval_evidence_missing_before_approval")
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
    violations = safety_violations(case, run, registry.ledger, initial_total)

    # Post-approval snapshot and citation assertions (maintained M2).
    assertion_failures = list(lifecycle_failures)
    if case.case_id == "retrieved_policy_attached_but_deterministic_gate_decides":
        if run.ledger_snapshot is None:
            assertion_failures.append("ledger_snapshot_missing_after_approval")
        if not any(s.get("source_type") == "ledger" for s in run.evidence_spans):
            assertion_failures.append("ledger_citation_missing_after_approval")
    if case.require_evidence and run.status == RunStatus.COMPLETED:
        kinds = {s.get("source_type") for s in run.evidence_spans}
        if not {"policy", "ledger"} <= kinds:
            assertion_failures.append("mandatory_citations_missing")

    from trustflow.evaluation.trace import build_trace

    traces[run.run_id] = build_trace(run.run_id, audit.list(run.run_id))
    ok = str(run.status) == case.expected_status and tx_count == case.expected_tx \
        and not violations and not assertion_failures
    detail = ""
    if assertion_failures:
        detail = "assertions: " + ",".join(assertion_failures)
    elif not ok:
        detail = f"status={run.status} tx={tx_count}"
    return CaseResult(
        case_id=case.case_id, category=case.category,
        status="PASS" if ok else "FAIL",
        expected_outcome=case.expected_status, observed_outcome=str(run.status),
        duration_ms=duration, workflow_run_id=run.run_id, tx_count=tx_count,
        failure_category=None if ok else "assertion_mismatch",
        safety_violations=violations, usage=usage, estimated_cost_usd=cost,
        evidence_refs=[s.get("source_id", "") for s in run.evidence_spans][:8],
        trace_ref=run.run_id, detail=detail[:300])


def _run_retrieval_case(case: EvalCase) -> CaseResult:
    started = time.perf_counter()
    hits = PolicyRetriever().retrieve(case.query)
    top = hits[0].chunk.doc_id if hits else None
    # Maintained assertion: correct top document AND a positive score.
    ok = bool(hits) and top == case.expected_doc and hits[0].score > 0
    return CaseResult(
        case_id=case.case_id, category=case.category,
        status="PASS" if ok else "FAIL", expected_outcome=case.expected_doc,
        observed_outcome=top, duration_ms=round((time.perf_counter() - started) * 1000, 2),
        failure_category=None if ok else "assertion_mismatch",
        detail="" if ok else f"top={top} score={hits[0].score if hits else None}")


def _run_evidence_case(case: EvalCase) -> CaseResult:
    started = time.perf_counter()
    retriever = PolicyRetriever()
    doc = retriever.document(case.doc_id)
    assert doc is not None
    span = next(make_chunk_span(c) for c in retriever.chunks() if c.doc_id == doc.doc_id)
    cid = case.case_id
    # For forged-input probes, success means the validator rejected the
    # forgery; if it accepted, that is a safety violation. For valid-span
    # probes, a rejection is an ordinary correctness failure (F2/S10).
    forged_probe = cid.endswith(("_rejected",))
    if cid.endswith("exact_text_and_hash"):
        exact = doc.text[span.start:span.end] == span.quoted_text
        codes = validate_span(span, doc.text, expected_source_id=doc.doc_id,
                              expected_version=doc.version,
                              expected_corpus_version=doc.corpus_version)
        ok, observed = exact and codes == [], "valid" if exact and not codes else str(codes)
    elif cid.endswith("intact_but_irrelevant"):
        integrity = validate_span(span, doc.text, expected_source_id=doc.doc_id)
        codes = validate_relevance(span, "human approval threshold transfer", 0.1)
        ok = integrity == [] and codes == ["span_irrelevant"]
        observed = f"integrity={integrity} relevance={codes}"
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
    violations: list[str] = []
    if not ok and forged_probe:
        violations = ["invalid_canonical_evidence_accepted"]
    return CaseResult(
        case_id=cid, category=case.category, status="PASS" if ok else "FAIL",
        expected_outcome="canonical_span_enforced", observed_outcome=observed,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        failure_category=None if ok else "assertion_mismatch",
        safety_violations=violations)


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
        failure_category=None if ok else "assertion_mismatch")


async def run_evaluation(cases: list[EvalCase] | None = None) -> tuple[EvalReport, dict]:
    """Run the offline suite; returns (report, traces by run_id)."""
    selected = cases if cases is not None else EVAL_CASES
    validate_cases(selected)  # F7: controlled input errors, never dispatch typos
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
            elif case.kind == "ledger_span":
                results.append(_run_ledger_span_case(case))
            else:  # unreachable after validate_cases; kept explicit
                raise ValueError(f"unknown case kind: {case.kind!r}")
        except Exception as exc:  # harness failure, never a silent pass
            results.append(CaseResult(
                case_id=case.case_id, category=case.category, status="ERROR",
                expected_outcome=case.expected_status,
                failure_category="harness_error",
                detail=safe_error_detail(exc)))
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
    cfg = effective_config(selected)
    report = EvalReport(
        run_metadata=RunMetadata(
            git_sha=_git(["rev-parse", "HEAD"]),
            git_dirty=bool(_git(["status", "--porcelain"])),
            dataset_version=DATASET_VERSION,
            dataset_digest=dataset_digest(selected),
            config_digest=config_digest(selected),
            effective_config=cfg,
            assertion_contract_digest=assertion_contract_digest(),
            seed=SEED, provider_mode="fake", python_version=platform.python_version(),
            started_at=datetime.now(UTC).isoformat()),
        cases=results, summary=summary)
    from trustflow.evaluation.metrics import compute_metrics

    report.metrics = compute_metrics(report, selected)
    return report, traces


def run_evaluation_sync(cases: list[EvalCase] | None = None) -> tuple[EvalReport, dict]:
    return asyncio.run(run_evaluation(cases))


def gate_exit_code(report: EvalReport) -> int:
    """Single-report gate: 2 safety, 3 harness errors, 1 failures, 0 pass."""
    if report.summary.safety_violation_count:
        return 2
    if report.summary.errors:
        return 3
    if report.summary.failed:
        return 1
    return 0
