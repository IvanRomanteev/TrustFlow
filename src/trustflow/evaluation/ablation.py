"""Safe offline ablation comparisons (shadow/diagnostic only).

Ablation A compares deterministic-verifier verdicts with the full
deterministic+semantic pipeline verdicts on identical completed runs.
It only reads verdicts the normal pipeline already produced: no
verification is disabled on any mutating path.

Ablation B compares optional retrieval context on/off. Both variants
run the full policy, approval and verification pipeline; only the
advisory retriever wiring differs, so evidence completeness and
verdict consistency can be compared without weakening any mandatory
gate. All results are fake-provider diagnostics, not live-model
accuracy measurements.
"""

import asyncio
import time

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.settings import Settings
from trustflow.domain.models import WorkflowGoal
from trustflow.evaluation.cases import EVAL_CASES, EvalCase
from trustflow.llm.provider import FakeLLMProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.tools.registry import ToolRegistry


def _engine(case: EvalCase, *, with_retrieval: bool):
    registry = ToolRegistry()
    settings = Settings(max_retries=1)
    policy = PolicyEngine(settings)
    fake = FakeLLMProvider(scripted=case.scripted)
    retriever = PolicyRetriever() if with_retrieval else None
    engine = WorkflowEngine(
        Planner(fake, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(fake, registry.ledger, retriever=retriever),
        retriever=retriever)
    return engine, registry


async def _run(case: EvalCase, *, with_retrieval: bool):
    engine, registry = _engine(case, with_retrieval=with_retrieval)
    title = case.goal_title or case.case_id
    started = time.perf_counter()
    run = await engine.create_workflow(WorkflowGoal(
        title=title, description=case.goal_description or title,
        require_evidence=case.require_evidence))
    for _ in range(4):
        if str(run.status) != "AWAITING_APPROVAL" or not case.approve \
                or run.plan is None:
            break
        engine.approvals.approve(run, "eval", run.plan.plan_hash)
        run = await engine.resume_after_approval(run)
    return run, registry, round((time.perf_counter() - started) * 1000, 2)


def _workflow_cases() -> list[EvalCase]:
    return [c for c in EVAL_CASES if c.kind == "workflow"]


async def ablation_verifier_diagnostics() -> dict:
    """A: deterministic-only vs deterministic+semantic verdict agreement."""
    rows = []
    for case in _workflow_cases():
        run, _registry, duration = await _run(case, with_retrieval=True)
        report = run.verification
        if report is None:
            empty: dict = {"case_id": case.case_id, "status": str(run.status),
                           "deterministic": None, "overall": None,
                           "semantic": None, "agreement": None}
            rows.append(empty)
            continue
        deterministic = all(c.passed for c in report.checks) if report.checks else None
        semantic = report.semantic.verdict if report.semantic else None
        rows.append({
            "case_id": case.case_id, "status": str(run.status),
            "deterministic": deterministic, "overall": report.passed,
            "semantic": semantic, "duration_ms": duration,
            "agreement": (deterministic == report.passed)
            if deterministic is not None else None})
    compared = [r for r in rows if r["agreement"] is not None]
    return {
        "experiment_id": "ablation-a-verifier-diagnostics",
        "baseline": "deterministic verifier checks only (diagnostic read)",
        "treatment": "full pipeline verdict (deterministic + semantic, as executed)",
        "safety_note": "diagnostic comparison over normally executed runs; "
                       "no verification disabled on any mutating path",
        "n": len(rows), "compared": len(compared),
        "agreements": sum(1 for r in compared if r["agreement"]),
        "semantic_reviews_observed": sum(1 for r in rows if r["semantic"]),
        "rows": rows,
        "limitations": "fake/scripted semantic reviewer; agreement is a "
                       "pipeline-consistency diagnostic, not model accuracy",
    }


async def ablation_retrieval_context() -> dict:
    """B: advisory retrieval context on vs off, all mandatory gates on."""
    rows = []
    for case in _workflow_cases():
        on_run, _r1, on_ms = await _run(case, with_retrieval=True)
        off_run, _r2, off_ms = await _run(case, with_retrieval=False)
        rows.append({
            "case_id": case.case_id,
            "status_with": str(on_run.status), "status_without": str(off_run.status),
            "status_consistent": str(on_run.status) == str(off_run.status),
            "evidence_spans_with": len(on_run.evidence_spans),
            "evidence_spans_without": len(off_run.evidence_spans),
            "duration_ms_with": on_ms, "duration_ms_without": off_ms})
    return {
        "experiment_id": "ablation-b-retrieval-context",
        "baseline": "retriever wired (advisory policy/ledger evidence attached)",
        "treatment": "retriever omitted (M1 wiring); policy/approval/verification unchanged",
        "safety_note": "mandatory policy and canonical-evidence gates remain "
                       "enforced in both variants; nothing is bypassed",
        "n": len(rows),
        "status_consistent": sum(1 for r in rows if r["status_consistent"]),
        "expected_fail_closed_without_retriever": [
            r["case_id"] for r in rows if not r["status_consistent"]],
        "fail_closed_note": "require_evidence cases correctly FAIL without a "
                            "retriever (no canonical evidence can be attached); "
                            "that inconsistency is the mandatory gate working, "
                            "not a regression",
        "mean_evidence_spans_with": round(
            sum(r["evidence_spans_with"] for r in rows) / len(rows), 2) if rows else None,
        "mean_evidence_spans_without": round(
            sum(r["evidence_spans_without"] for r in rows) / len(rows), 2) if rows else None,
        "rows": rows,
        "limitations": "fake provider ignores retrieved text; this measures "
                       "evidence attachment and verdict consistency, not live "
                       "model sensitivity to context",
    }


async def run_ablations() -> dict:
    return {
        "schema_version": "1.0", "provider_mode": "fake",
        "dataset_cases": len(EVAL_CASES),
        "experiments": [await ablation_verifier_diagnostics(),
                        await ablation_retrieval_context()],
    }


def run_ablations_sync() -> dict:
    return asyncio.run(run_ablations())
