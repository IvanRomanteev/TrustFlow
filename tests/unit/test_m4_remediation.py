"""M4 remediation round 1: negative tests for F1-F8.

Each test reproduces an independently confirmed finding against the
remediated layer. Offline, fake provider only.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import trustflow.evaluation.runner as runner
from trustflow.domain.models import RunStatus, VerificationCheck
from trustflow.evaluation.cases import CASES_BY_ID, EVAL_CASES, validate_cases
from trustflow.evaluation.metrics import compute_metrics
from trustflow.evaluation.schemas import CaseResult, EvalReport
from trustflow.llm.usage import LLMCallRecord, estimate_cost
from trustflow.retrieval.corpus import PolicyRetriever

SRC = Path(__file__).resolve().parents[1].parent / "src"


def _one(case):
    report, _ = runner.run_evaluation_sync([case])
    return report


def _fault(case_id, mutation, method="create_workflow"):
    original = getattr(runner.WorkflowEngine, method)

    async def wrapper(self, *a, **kw):
        run = await original(self, *a, **kw)
        mutation(self, run)
        return run

    with patch.object(runner.WorkflowEngine, method, wrapper):
        return _one(CASES_BY_ID[case_id])


# --- F1: full maintained assertion semantics -------------------------------

def test_f1_missing_mandatory_citations_fail():
    real = runner.WorkflowEngine.create_workflow

    async def strip(self, *a, **kw):
        run = await real(self, *a, **kw)
        if run.status == RunStatus.COMPLETED:
            run.evidence_spans = []
        return run

    with patch.object(runner.WorkflowEngine, "create_workflow", strip):
        report = _one(CASES_BY_ID[
            "mandatory_evidence_workflow_completes_with_citations"])
    assert report.cases[0].status == "FAIL"
    assert runner.gate_exit_code(report) != 0


def test_f1_zero_retrieval_score_fails():
    retrieve = PolicyRetriever.retrieve

    def zero(self, query):
        return [h.model_copy(update={"score": 0.0}) for h in retrieve(self, query)]

    with patch.object(PolicyRetriever, "retrieve", zero):
        report = _one(CASES_BY_ID["policy_retrieval_approval_threshold"])
    assert report.cases[0].status == "FAIL"


def test_f1_registry_matches_maintained_fixtures_semantically():
    def load(name, rel):
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[1] / rel)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    m1 = load("rem_m1", "eval/test_scenarios.py")
    for name, steps, expected, approve, tx in m1.SCENARIOS:
        case = CASES_BY_ID[name]
        assert (case.scripted, case.expected_status, case.approve,
                case.expected_tx) == (steps, str(expected), approve, tx)


# --- F2: authoritative safety detector --------------------------------------

def test_f2_s01_completed_without_verification():
    report = _fault("small_transfer_50",
                    lambda e, r: setattr(r, "verification", None))
    assert "missing_verification" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_f2_s02_inconsistent_verification_report():
    def mutate(engine, run):
        run.verification.checks.append(
            VerificationCheck(name="probe_failed", passed=False))
        run.verification.semantic.verdict = "FAIL"

    report = _fault("small_transfer_50", mutate)
    assert "inconsistent_verification_report" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_f2_s03_missing_approval_provenance():
    def mutate(engine, run):
        run.approvals = []
        run.approved_hash = ""
        run.approved_plan_snapshot = None

    report = _fault("high_1001_needs_approval", mutate,
                    method="resume_after_approval")
    assert "unauthorized_approval_acceptance" in report.cases[0].safety_violations
    assert report.metrics["safety"]["unauthorized_approval_acceptances"] == 1


def test_f2_s04_s05_s06_ledger_state_detected():
    def wrong_key(engine, run):
        ledger = engine.executor._registry.ledger
        ledger.transactions[0].idempotency_key = "foreign-operation"
        ledger._by_idempotency = {t.idempotency_key: t for t in ledger.transactions}

    report = _fault("small_transfer_50", wrong_key)
    assert "ledger_state_inconsistent" in report.cases[0].safety_violations

    def duplicate(engine, run):
        ledger = engine.executor._registry.ledger
        ledger.transactions[1] = ledger.transactions[0].model_copy(
            update={"tx_id": "tx_duplicate"})
        ledger._by_idempotency = {t.idempotency_key: t for t in ledger.transactions}

    report = _fault("two_transfers_distinct_ops", duplicate)
    assert "duplicate_committed_operation" in report.cases[0].safety_violations

    def balance(engine, run):
        engine.executor._registry.ledger.accounts["acc_alice"].balance += 1

    report = _fault("balance_check", balance)
    assert "ledger_state_inconsistent" in report.cases[0].safety_violations


def test_f2_s07_forged_mandatory_span():
    def forge(engine, run):
        run.evidence_spans[0]["text_hash"] = "f" * 64

    report = _fault("mandatory_evidence_workflow_completes_with_citations", forge)
    assert "invalid_mandatory_evidence" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_f2_s10_valid_span_rejection_is_ordinary_failure():
    with patch.object(runner, "validate_span", return_value=["span_hash_mismatch"]):
        report = _one(CASES_BY_ID["evidence_span_exact_text_and_hash"])
    assert report.cases[0].status == "FAIL"
    assert report.cases[0].safety_violations == []
    assert runner.gate_exit_code(report) == 1


# --- F3/F8: per-record usage and cost ----------------------------------------

def _record(model="muse-wonder-1.0", it=100, ot=10, **kw):
    return LLMCallRecord(purpose="plan", model=model,
                         input_tokens=it, output_tokens=ot, **kw)


def _usage(records):
    return runner._usage_from(
        SimpleNamespace(llm_calls=[r.model_dump() for r in records]))


def test_f3_partial_unknown_total_cost_null_with_partial_subtotal():
    usage, cost = _usage([_record(), _record(model="fake", it=None, ot=None,
                                             attempt=2)])
    assert usage.usage_unknown and cost is None
    assert usage.partial_known_cost_usd == pytest.approx(
        estimate_cost("muse-wonder-1.0", 100, 10))


def test_f3_complementary_unknown_records_total_null():
    _, cost = _usage([_record(ot=None), _record(it=None, attempt=2)])
    assert cost is None


def test_f3_mixed_models_priced_per_record():
    records = [_record(), _record(model="muse-spark-1.3", attempt=2)]
    _, cost = _usage(records)
    expected = sum(estimate_cost(r.model, r.input_tokens, r.output_tokens)
                   for r in records)
    assert cost == pytest.approx(expected)
    usage, _ = _usage(records)
    assert usage.models == ["muse-spark-1.3", "muse-wonder-1.0"]
    assert usage.model is None


def test_f3_unpriced_record_makes_total_null_any_order():
    assert _usage([_record(), _record(model="unpriced-x", attempt=2)])[1] is None
    assert _usage([_record(model="unpriced-x"),
                   _record(attempt=2)])[1] is None


def test_f3_empty_and_fake_unknown_and_zero():
    usage, cost = _usage([])
    assert (usage.logical_calls, usage.transport_attempts, cost) == (0, 0, None)
    usage, cost = _usage([_record(model="fake", it=None, ot=None)])
    assert usage.usage_unknown and cost is None
    usage, cost = _usage([_record(it=0, ot=0)])
    assert (usage.input_tokens, usage.output_tokens, cost) == (0, 0, 0.0)


def test_f8_known_zero_aggregate_not_null():
    usage, _ = _usage([_record(it=0, ot=0)])
    case = CASES_BY_ID["balance_check"]
    metrics = compute_metrics(
        EvalReport(cases=[CaseResult(case_id=case.case_id, category="workflow",
                                     status="PASS", usage=usage)]), [case])
    assert metrics["usage"]["input_tokens_known_sum"] == 0
    assert metrics["usage"]["output_tokens_known_sum"] == 0


# --- F6: pinned effective configuration --------------------------------------

def test_f6_pinned_settings_ignore_ambient_env(monkeypatch):
    monkeypatch.setenv("TRUSTFLOW_HIGH_RISK_THRESHOLD", "1000000000")
    settings = runner.pinned_settings()
    assert settings.high_risk_threshold == 1000.0
    assert runner.config_digest(EVAL_CASES) == runner.config_digest(EVAL_CASES)
    subset = runner.config_digest(EVAL_CASES[:1])
    assert subset != runner.config_digest(EVAL_CASES)


# --- F7: invalid inputs --------------------------------------------------------

def test_f7_invalid_suites_rejected():
    base = CASES_BY_ID["balance_check"]
    with pytest.raises(ValueError):
        validate_cases([])
    with pytest.raises(ValueError):
        validate_cases([base, base.model_copy(deep=True)])
    with pytest.raises(ValueError):
        validate_cases([base.model_copy(update={"kind": "typo-kind"})])
    with pytest.raises(ValueError):
        runner.run_evaluation_sync(
            [base.model_copy(update={"kind": "typo-kind"})])


# --- F4/F5: real CLI subprocess lifecycle -------------------------------------

_FIXTURE = '''
import runpy, sys
import trustflow.evaluation.runner as runner
import trustflow.evaluation.report as writers
from trustflow.evaluation.cases import CASES_BY_ID
mode, args = sys.argv[1], sys.argv[2:]
base = CASES_BY_ID["balance_check"].model_copy(deep=True)
pay = CASES_BY_ID["small_transfer_50"].model_copy(deep=True)
runner.EVAL_CASES = [base]
if mode == "case_exception":
    async def explode(case, traces):
        raise RuntimeError("api_key=PROBESECRET12345")
    runner._run_workflow_case = explode
elif mode == "outer_exception":
    def bad():
        raise RuntimeError("api_key=PROBESECRET12345")
    runner.run_evaluation_sync = bad
elif mode == "write_partial":
    real = writers._atomic_write
    def partial(path, text):
        if path.name == "traces.json":
            raise OSError("simulated disk error")
        return real(path, text)
    writers._atomic_write = partial
elif mode == "repeat_safety_then_pass":
    real_run = runner.run_evaluation_sync
    calls = {"n": 0}
    def alternating(cases=None):
        calls["n"] += 1
        case = pay.model_copy(update={"expected_tx": 0}) if calls["n"] == 1 else pay
        return real_run([case])
    runner.run_evaluation_sync = alternating
sys.argv = ["trustflow.evaluation", *args]
runpy.run_module("trustflow.evaluation", run_name="__main__")
'''


def _cli(tmp_path, mode, *args):
    fixture = tmp_path / "cli_fixture_probe.py"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{SRC}"
    env["TRUSTFLOW_LLM_MODE"] = "fake"
    return subprocess.run(
        [sys.executable, str(fixture), mode, *args],
        capture_output=True, text=True, env=env, timeout=300)


def test_f4_case_exception_sanitized_everywhere(tmp_path):
    out = tmp_path / "o"
    proc = _cli(tmp_path, "case_exception", "--out", str(out))
    assert proc.returncode == 3
    blob = proc.stdout + proc.stderr
    for path in out.rglob("*") if out.exists() else []:
        if path.is_file():
            blob += path.read_text(encoding="utf-8", errors="ignore")
    assert "PROBESECRET12345" not in blob


def test_f4_outer_exception_exit3_no_traceback_marker(tmp_path):
    proc = _cli(tmp_path, "outer_exception", "--out", str(tmp_path / "o"))
    assert proc.returncode == 3
    assert "PROBESECRET12345" not in proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr


def test_f5_write_partial_publishes_nothing(tmp_path):
    out = tmp_path / "o"
    proc = _cli(tmp_path, "write_partial", "--out", str(out))
    assert proc.returncode == 3
    assert not out.exists() or not (out / "COMPLETE.json").exists()
    assert not (out / "evaluation-results.json").exists()


def test_f5_repeat_safety_preserved_across_repeats(tmp_path):
    out = tmp_path / "o"
    proc = _cli(tmp_path, "repeat_safety_then_pass", "--out", str(out),
                "--repeat", "2")
    assert proc.returncode == 2
    repeats = json.loads((out / "repeats.json").read_text(encoding="utf-8"))
    assert repeats["repeats"][0]["safety_violations"] == 1
    assert json.loads(proc.stdout)["safety_violations"] == 1


def test_f5_invalid_repeat_is_usage_error(tmp_path):
    fixture = tmp_path / "noop.py"
    fixture.write_text("import sys\nsys.argv=['x','--repeat','nope']\n"
                       "from trustflow.evaluation.__main__ import main\n"
                       "raise SystemExit(main())\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run([sys.executable, str(fixture)], capture_output=True,
                          text=True, env=env, timeout=120)
    assert proc.returncode == 64
