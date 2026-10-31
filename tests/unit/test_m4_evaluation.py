"""M4 evaluation/observability tests: offline, fake provider only."""

import importlib.util
from pathlib import Path

import pytest

from trustflow.audit.logger import AuditLogger
from trustflow.evaluation.ablation import run_ablations_sync
from trustflow.evaluation.cases import CASES_BY_ID, EVAL_CASES, dataset_digest
from trustflow.evaluation.report import render_csv, render_markdown, write_reports
from trustflow.evaluation.runner import gate_exit_code, run_evaluation_sync
from trustflow.evaluation.schemas import CaseResult, EvalReport, EvalSummary
from trustflow.evaluation.trace import build_trace, render_timeline


def _load_module(name: str, rel: str):
    path = Path(__file__).resolve().parents[1] / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def suite():
    return run_evaluation_sync()


def test_all_33_maintained_cases_preserved():
    m1 = _load_module("eval_scenarios", "eval/test_scenarios.py")
    m2 = _load_module("eval_m2", "eval/test_m2_scenarios.py")
    registry = set(CASES_BY_ID)
    assert len(EVAL_CASES) == 33
    assert {s[0] for s in m1.SCENARIOS} <= registry
    m2_ids = {n[5:] for n in dir(m2) if n.startswith("test_")}
    assert len(m2_ids) == 11
    assert m2_ids <= registry
    assert len(registry) == 33


def test_case_ids_unique_and_digest_stable():
    assert len(CASES_BY_ID) == len(EVAL_CASES)
    assert dataset_digest() == dataset_digest()


def test_suite_all_pass_zero_safety_violations(suite):
    report, traces = suite
    assert report.summary.total == 33
    assert report.summary.passed == 33
    assert report.summary.safety_violation_count == 0
    assert report.summary.critical_passed == report.summary.critical_total
    assert gate_exit_code(report) == 0
    assert traces  # workflow cases produced correlated traces


def test_reproducibility_verdict_digest(suite):
    report, _ = suite
    again, _ = run_evaluation_sync()
    assert again.verdict_digest() == report.verdict_digest()


def test_unknown_usage_never_reported_as_zero(suite):
    report, _ = suite
    workflow = [c for c in report.cases if c.workflow_run_id]
    assert workflow
    for case in workflow:
        # Fake provider reports no tokens: unknown must stay null.
        assert case.usage.input_tokens is None
        assert case.usage.usage_unknown is True
        assert case.estimated_cost_usd is None
    assert report.metrics["usage"]["missing_usage_rate"] == 1.0
    assert report.metrics["usage"]["known_estimated_cost_usd"] is None


def test_gate_exit_codes():
    base = EvalReport(summary=EvalSummary(total=1, passed=1))
    assert gate_exit_code(base) == 0
    failed = EvalReport(summary=EvalSummary(total=1, failed=1))
    assert gate_exit_code(failed) == 1
    violated = EvalReport(summary=EvalSummary(total=1, passed=1,
                                              safety_violation_count=1))
    assert gate_exit_code(violated) == 2
    errored = EvalReport(summary=EvalSummary(total=1, errors=1))
    assert gate_exit_code(errored) == 3


def test_trace_correlation_and_redaction():
    audit = AuditLogger()
    audit.log("run-1", "planner", "plan_created", {"api_key": "sk-secret-123"})
    audit.log("run-1", "executor", "execution_finished", {"ok": True})
    trace = build_trace("run-1", audit.list("run-1"))
    assert trace["trace_id"] == "run-1"
    assert [s["stage"] for s in trace["spans"]] == ["planning", "execution"]
    assert trace["spans"][1]["parent_span_id"] == "run-1:0"
    blob = str(trace)
    assert "sk-secret-123" not in blob and "[REDACTED]" in blob
    assert "Trace run-1" in render_timeline(trace)
    empty = build_trace("run-x", [])
    assert empty["spans"] == []  # missing trace is empty, never fabricated


def test_reports_written_and_csv_rows(suite, tmp_path):
    report, traces = suite
    paths = write_reports(report, traces, tmp_path)
    for key in ("json", "csv", "markdown", "traces", "manifest"):
        assert Path(paths[key]).exists()
    csv_text = Path(paths["csv"]).read_text(encoding="utf-8")
    assert csv_text.count("\n") >= 34  # header + 33 cases
    assert "small_transfer_50" in render_markdown(report)
    assert render_csv(report).splitlines()[1].startswith("balance_check")


def test_ablations_are_shadow_only_and_consistent():
    result = run_ablations_sync()
    a, b = result["experiments"]
    assert a["compared"] > 0 and a["agreements"] == a["compared"]
    assert b["status_consistent"] >= b["n"] - 1
    assert b["expected_fail_closed_without_retriever"] == [
        "mandatory_evidence_workflow_completes_with_citations"]
    assert "no verification disabled" in a["safety_note"]


def test_schema_version_and_case_result_shape(suite):
    report, _ = suite
    assert report.schema_version == "1.0"
    case = report.cases[0]
    assert isinstance(case, CaseResult)
    assert case.verdict_key()[0] == case.case_id
