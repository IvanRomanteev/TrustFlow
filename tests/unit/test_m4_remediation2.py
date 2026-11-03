"""M4 remediation round 2: negative tests for R1-F2, R1-F4, R1-F5."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import trustflow.evaluation.runner as runner
from trustflow.domain.models import ApprovalDecision
from trustflow.evaluation.cases import CASES_BY_ID

SRC = Path(__file__).resolve().parents[1].parent / "src"
MARK = "REVIEWSECRET0001"


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


# --- R1-F2: S11-S15 ---------------------------------------------------------

def test_s11_semantic_fail_hidden_by_summary():
    def mutate(engine, run):
        run.verification.semantic.verdict = "FAIL"

    report = _fault("small_transfer_50", mutate)
    assert "inconsistent_verification_report" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_s12_rejected_approval_record():
    def mutate(engine, run):
        run.approvals[0].decision = ApprovalDecision.REJECTED

    report = _fault("high_1001_needs_approval", mutate,
                    method="resume_after_approval")
    assert "unauthorized_approval_acceptance" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_s13_foreign_approval_plan_hash():
    def mutate(engine, run):
        run.approvals[0].plan_hash = "f" * 64

    report = _fault("high_1001_needs_approval", mutate,
                    method="resume_after_approval")
    assert "unauthorized_approval_acceptance" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_s14_foreign_mandatory_citation_source():
    def mutate(engine, run):
        for span in run.evidence_spans:
            if span.get("source_type") == "policy":
                span["source_id"] = "policy-foreign"
                break

    report = _fault("mandatory_evidence_workflow_completes_with_citations", mutate)
    assert "invalid_mandatory_evidence" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


def test_s15_failed_workflow_balance_mutation_without_transactions():
    def mutate(engine, run):
        engine.executor._registry.ledger.accounts["acc_alice"].balance += 1

    report = _fault("unknown_tool", mutate)
    assert report.cases[0].tx_count == 0
    assert "ledger_state_inconsistent" in report.cases[0].safety_violations
    assert runner.gate_exit_code(report) == 2


# --- R1-F4 / R1-F5: real CLI subprocess --------------------------------------

_FIXTURE = '''
import runpy, sys
from pathlib import Path
import trustflow.evaluation.runner as runner
import trustflow.evaluation.report as writers
import trustflow.evaluation.ablation as ablation
from trustflow.evaluation.cases import CASES_BY_ID
mode, args = sys.argv[1], sys.argv[2:]
base = CASES_BY_ID["balance_check"].model_copy(deep=True)
pay = CASES_BY_ID["small_transfer_50"].model_copy(deep=True)
runner.EVAL_CASES = [base]
MARK = "REVIEWSECRET0001"
async def explode(case, traces):
    raise RuntimeError("api_key=" + MARK)
if mode == "assertion":
    runner.EVAL_CASES = [base.model_copy(update={"expected_status": "FAILED"})]
elif mode == "case_exception":
    runner._run_workflow_case = explode
elif mode == "boundary_payload":
    real = runner.run_evaluation_sync
    def raw(cases=None):
        report, traces = real(cases)
        report.cases[0].detail = "api_key=" + MARK
        report.cases[0].observed_outcome = "api_key=" + MARK
        report.metrics["diagnostic"] = {"nested": [{"api_key": MARK}]}
        report.run_metadata.effective_config["diagnostic"] = {"api_key": MARK}
        for trace in traces.values():
            if trace["spans"]:
                trace["spans"][0]["detail"] = {"nested": [{"api_key": MARK}]}
                trace["spans"][0]["event"] = "api_key=" + MARK
        return report, traces
    runner.run_evaluation_sync = raw
elif mode == "ablation_payload":
    ablation.run_ablations_sync = lambda: {"diagnostic": {"api_key": MARK}}
elif mode == "safety_write_partial":
    runner.EVAL_CASES = [pay.model_copy(update={"expected_tx": 0})]
    real = writers._atomic_write
    def partial(path, text):
        if path.name == "traces.json":
            raise OSError("api_key=" + MARK)
        return real(path, text)
    writers._atomic_write = partial
elif mode == "safety_ablation_exception":
    runner.EVAL_CASES = [pay.model_copy(update={"expected_tx": 0})]
    def bad():
        raise RuntimeError("api_key=" + MARK)
    ablation.run_ablations_sync = bad
elif mode == "repeat_safety_then_exception":
    real = runner.run_evaluation_sync
    calls = {"n": 0}
    def alternating(cases=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("api_key=" + MARK)
        return real([pay.model_copy(update={"expected_tx": 0})])
    runner.run_evaluation_sync = alternating
sys.argv = ["trustflow.evaluation", *args]
runpy.run_module("trustflow.evaluation", run_name="__main__")
'''


def _write_fixture(tmp_path):
    fixture = tmp_path / "r2_fixture.py"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    return fixture


def _cli(tmp_path, fixture, mode, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{SRC}"
    env["TRUSTFLOW_LLM_MODE"] = "fake"
    return subprocess.run([sys.executable, str(fixture), mode, *args],
                          capture_output=True, text=True, env=env, timeout=300)


def _marker_hits(root: Path) -> list[str]:
    hits = []
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file() and MARK.encode() in path.read_bytes():
                hits.append(str(path))
    return hits


def test_f4_boundary_payload_no_marker_anywhere(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    proc = _cli(tmp_path, fixture, "boundary_payload", "--out", str(out))
    assert proc.returncode == 0
    assert _marker_hits(out) == []
    assert MARK not in proc.stdout + proc.stderr
    data = json.loads((out / "evaluation-results.json").read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 1
    json.loads((out / "COMPLETE.json").read_text(encoding="utf-8"))


def test_f4_ablation_payload_no_marker(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    proc = _cli(tmp_path, fixture, "ablation_payload", "--out", str(out),
                "--ablations")
    assert proc.returncode == 0
    assert _marker_hits(out) == []
    json.loads((out / "ablations.json").read_text(encoding="utf-8"))


def test_f5_safety_survives_write_and_ablation_failures(tmp_path):
    fixture = _write_fixture(tmp_path)
    proc = _cli(tmp_path, fixture, "safety_write_partial",
                "--out", str(tmp_path / "a"))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["safety_violations"] == 1
    proc = _cli(tmp_path, fixture, "safety_ablation_exception",
                "--out", str(tmp_path / "b"), "--ablations")
    assert proc.returncode == 2
    proc = _cli(tmp_path, fixture, "repeat_safety_then_exception",
                "--out", str(tmp_path / "c"), "--repeat", "2")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["safety_violations"] == 1


def test_f5_previous_pass_preserved_on_later_failures(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    first = _cli(tmp_path, fixture, "normal", "--out", str(out))
    assert first.returncode == 0
    before = (out / "evaluation-results.json").read_bytes()
    second = _cli(tmp_path, fixture, "case_exception", "--out", str(out))
    assert second.returncode == 3
    assert (out / "evaluation-results.json").read_bytes() == before
    siblings = [p for p in tmp_path.iterdir()
                if p.name.startswith("o.failed-")]
    assert siblings and (siblings[0] / "COMPLETE.json").exists()
    third = _cli(tmp_path, fixture, "assertion", "--out", str(out))
    assert third.returncode == 1
    assert (out / "evaluation-results.json").read_bytes() == before


def test_f5_unrelated_staging_sentinel_not_deleted(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    stray = tmp_path / "o.staging-999999"
    stray.mkdir()
    sentinel = stray / "unrelated-user-file.txt"
    sentinel.write_text("preserve me", encoding="utf-8")
    _cli(tmp_path, fixture, "case_exception", "--out", str(out))
    assert sentinel.read_text(encoding="utf-8") == "preserve me"
