"""M4 remediation round 3: R2-F4a keys, R2-F4b stdout, R2-F5 evidence."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from trustflow.evaluation.errors import sanitize_payload

SRC = Path(__file__).resolve().parents[1].parent / "src"
MARK = "REVIEWSECRET0001"


# --- R2-F4a: mapping keys ----------------------------------------------------

def test_f4a_secret_bearing_metadata_key_is_sanitized():
    payload = {"nested": {f"Authorization Bearer {MARK}":
                          "untrusted header diagnostic"},
               "input_tokens": 123, "output_tokens": 17,
               "token_budget": 5000, "total": 3}
    before = copy.deepcopy(payload)
    cleaned = sanitize_payload(payload)
    assert MARK not in json.dumps(cleaned)
    assert cleaned["input_tokens"] == 123
    assert cleaned["output_tokens"] == 17
    assert cleaned["token_budget"] == 5000
    assert cleaned["total"] == 3
    assert payload == before  # authoritative object not mutated


def test_f4a_unsupported_key_never_calls_untrusted_str():
    class Evil:
        def __str__(self):
            raise AssertionError("__str__ called")

        def __repr__(self):
            raise AssertionError("__repr__ called")

    cleaned = sanitize_payload({Evil(): "x", 7: "seven", None: "n"})
    assert cleaned["<unsupported-key>"] == "x"
    assert cleaned["7"] == "seven"


def test_f4a_sanitized_key_collision_is_not_silent():
    cleaned = sanitize_payload(
        {"Authorization Bearer AAA111": 1, "Authorization Bearer BBB222": 2})
    assert MARK not in json.dumps(cleaned)
    assert len(cleaned) == 2
    assert any("<collision-" in key for key in cleaned)


# --- CLI subprocess fixture --------------------------------------------------

_FIXTURE = '''
import copy, runpy, sys
import trustflow.evaluation.runner as runner
import trustflow.evaluation.report as writers
import trustflow.evaluation.ablation as ablation
from trustflow.evaluation.cases import CASES_BY_ID
mode, args = sys.argv[1], sys.argv[2:]
MARK = "REVIEWSECRET0001"
base = CASES_BY_ID["balance_check"]
pay = CASES_BY_ID["small_transfer_50"]
runner.EVAL_CASES = [base]
real = runner.run_evaluation_sync
if mode == "metadata_key":
    def raw(cases=None):
        report, traces = real(cases)
        payload = {"nested": {"Authorization Bearer " + MARK:
                              "untrusted header diagnostic"}}
        report.metrics["diagnostic"] = payload
        report.run_metadata.effective_config["diagnostic"] = copy.deepcopy(payload)
        for trace in traces.values():
            if trace["spans"]:
                trace["spans"][0]["detail"] = copy.deepcopy(payload)
        return report, traces
    runner.run_evaluation_sync = raw
elif mode == "stdout_metadata":
    def raw2(cases=None):
        report, traces = real(cases)
        report.run_metadata.config_digest = "api_key=" + MARK
        return report, traces
    runner.run_evaluation_sync = raw2
elif mode in ("repeat_safety_then_exception", "repeat_fail_then_exception"):
    calls = {"n": 0}
    first = (pay.model_copy(update={"expected_tx": 0}) if mode.startswith("repeat_safety")
             else base.model_copy(update={"expected_status": "FAILED"}))
    def alternating(cases=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("api_key=" + MARK)
        return real([first])
    runner.run_evaluation_sync = alternating
elif mode == "safety_writer_failure":
    runner.EVAL_CASES = [pay.model_copy(update={"expected_tx": 0})]
    real_write = writers._atomic_write
    def partial(path, text):
        if path.name == "traces.json":
            raise OSError("api_key=" + MARK)
        return real_write(path, text)
    writers._atomic_write = partial
elif mode == "safety_ablation_failure":
    runner.EVAL_CASES = [pay.model_copy(update={"expected_tx": 0})]
    def bad():
        raise RuntimeError("api_key=" + MARK)
    ablation.run_ablations_sync = bad
sys.argv = ["trustflow.evaluation", *args]
runpy.run_module("trustflow.evaluation", run_name="__main__")
'''


def _write_fixture(tmp_path):
    fixture = tmp_path / "r3_fixture.py"
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


def _all_text(root: Path) -> str:
    parts = []
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file() and path.name != "r3_fixture.py":
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _tmp_text(tmp_path: Path) -> str:
    return _all_text(tmp_path)


def test_f4a_cli_metadata_key_no_marker_anywhere(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    proc = _cli(tmp_path, fixture, "metadata_key", "--out", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [h for h in _marker_hits(tmp_path) if "r3_fixture" not in h] == []
    assert MARK not in proc.stdout + proc.stderr
    data = json.loads((out / "evaluation-results.json").read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 1
    json.loads((out / "COMPLETE.json").read_text(encoding="utf-8"))
    json.loads((out / "reproducibility-manifest.json").read_text(encoding="utf-8"))


def test_f4b_cli_stdout_uses_finalized_safe_provenance(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    proc = _cli(tmp_path, fixture, "stdout_metadata", "--out", str(out))
    assert MARK not in proc.stdout + proc.stderr
    assert [h for h in _marker_hits(tmp_path) if "r3_fixture" not in h] == []
    # Malformed provenance is rejected as a harness error, not
    # published as a successful reproducibility claim.
    assert proc.returncode == 3
    assert not (out / "COMPLETE.json").exists()
    payload = json.loads(proc.stdout)
    assert payload["published"] is False
    assert payload["publication_complete"] is False


def test_f5_repeat_safety_then_exception_retains_evidence(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    proc = _cli(tmp_path, fixture, "repeat_safety_then_exception",
                "--out", str(out), "--repeat", "2")
    assert proc.returncode == 2
    assert MARK not in proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["safety_violations"] == 1
    assert payload["repeats_completed"] == 1
    combined = proc.stdout + proc.stderr + _tmp_text(tmp_path)
    assert "small_transfer_50" in combined
    assert "unexpected_ledger_mutation" in combined
    assert payload["completed_repeats"][0]["verdict_digest"] in combined
    assert payload["completed_repeats"][0]["git_sha"]
    assert payload["attempt"]
    assert not (out / "COMPLETE.json").exists()
    failed = [p for p in tmp_path.iterdir() if p.name.startswith("o.failed-")]
    assert failed and (failed[0] / "FAILED_ATTEMPT.json").exists()
    assert not (failed[0] / "COMPLETE.json").exists()


def test_f5_writer_and_ablation_failures_retain_evidence(tmp_path):
    fixture = _write_fixture(tmp_path)
    proc = _cli(tmp_path, fixture, "safety_writer_failure",
                "--out", str(tmp_path / "a"))
    assert proc.returncode == 2
    assert "unexpected_ledger_mutation" in proc.stdout + _tmp_text(tmp_path)
    assert json.loads(proc.stdout)["safety_violations"] == 1
    proc = _cli(tmp_path, fixture, "safety_ablation_failure",
                "--out", str(tmp_path / "b"), "--ablations")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["safety_violations"] == 1
    assert "small_transfer_50" in proc.stdout + _tmp_text(tmp_path)


def test_f5_failed_case_then_exception_and_normal_repeat(tmp_path):
    fixture = _write_fixture(tmp_path)
    proc = _cli(tmp_path, fixture, "repeat_fail_then_exception",
                "--out", str(tmp_path / "c"), "--repeat", "2")
    assert proc.returncode == 3
    assert "balance_check" in proc.stdout + _tmp_text(tmp_path)
    out = tmp_path / "ok"
    proc = _cli(tmp_path, fixture, "normal", "--out", str(out), "--repeat", "2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert len(payload["repeats"]) == 2
    assert payload["config_digest"]
    manifest = json.loads(
        (out / "reproducibility-manifest.json").read_text(encoding="utf-8"))
    assert manifest["config_digest"] == payload["config_digest"]
    complete = json.loads((out / "COMPLETE.json").read_text(encoding="utf-8"))
    assert complete["config_digest"] == payload["config_digest"]


def test_f5_late_failure_preserves_previous_success(tmp_path):
    fixture = _write_fixture(tmp_path)
    out = tmp_path / "o"
    first = _cli(tmp_path, fixture, "normal", "--out", str(out))
    assert first.returncode == 0
    before = (out / "evaluation-results.json").read_bytes()
    complete_before = (out / "COMPLETE.json").read_bytes()
    second = _cli(tmp_path, fixture, "repeat_safety_then_exception",
                  "--out", str(out), "--repeat", "2")
    assert second.returncode == 2
    assert (out / "evaluation-results.json").read_bytes() == before
    assert (out / "COMPLETE.json").read_bytes() == complete_before
    assert "unexpected_ledger_mutation" in second.stdout + _tmp_text(tmp_path)
