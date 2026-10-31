"""M4 remediation round 4: custom numeric/string key subclasses (R3-F4a-INT)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from trustflow.evaluation.errors import sanitize_payload

SRC = Path(__file__).resolve().parents[1].parent / "src"
MARK = "REVIEWSECRET0001"


class CustomInt(int):
    calls = 0

    def __str__(self):
        type(self).calls += 1
        return "Authorization Bearer " + MARK

    def __repr__(self):
        type(self).calls += 1
        return "Authorization Bearer " + MARK


class CustomFloat(float):
    calls = 0

    def __str__(self):
        type(self).calls += 1
        return "Authorization Bearer " + MARK

    def __repr__(self):
        type(self).calls += 1
        return "Authorization Bearer " + MARK


class CustomStr(str):
    calls = 0

    def __str__(self):
        type(self).calls += 1
        return "Authorization Bearer " + MARK

    def __repr__(self):
        type(self).calls += 1
        return "Authorization Bearer " + MARK


def setup_function():
    CustomInt.calls = CustomFloat.calls = CustomStr.calls = 0


def test_t1_custom_int_key_hook_never_runs():
    cleaned = sanitize_payload({CustomInt(7): "diagnostic"})
    assert CustomInt.calls == 0
    assert cleaned == {"<unsupported-key>": "diagnostic"}
    assert MARK not in json.dumps(cleaned)
    json.dumps(cleaned)  # safely serializable


def test_t2_custom_float_key_hook_never_runs():
    cleaned = sanitize_payload({CustomFloat(1.5): "diagnostic"})
    assert CustomFloat.calls == 0
    assert cleaned == {"<unsupported-key>": "diagnostic"}
    assert MARK not in json.dumps(cleaned)


def test_t3_custom_str_key_hook_never_runs():
    cleaned = sanitize_payload({CustomStr("label"): "diagnostic"})
    assert CustomStr.calls == 0
    assert cleaned == {"<unsupported-key>": "diagnostic"}
    assert MARK not in json.dumps(cleaned)


def test_t4_ordinary_numeric_keys_preserved():
    cleaned = sanitize_payload(
        {7: "a", 1.5: "b", True: "c", None: "d", "input_tokens": 123})
    assert cleaned["7"] == "a"
    assert cleaned["1.5"] == "b"
    assert cleaned["True"] == "c"
    assert cleaned["None"] == "d"
    assert cleaned["input_tokens"] == 123


def test_t5_unsupported_collision_no_silent_overwrite():
    cleaned = sanitize_payload({
        CustomInt(1): "first", CustomFloat(2.5): "second",
        CustomStr("x"): "third", "measurement": 42})
    assert CustomInt.calls == 0 and CustomFloat.calls == 0
    assert CustomStr.calls == 0
    assert len(cleaned) == 4
    assert cleaned["measurement"] == 42
    keys = list(cleaned)
    assert keys[0] == "<unsupported-key>"
    assert keys[1] == "<unsupported-key> <collision-2>"
    assert keys[2] == "<unsupported-key> <collision-3>"
    assert MARK not in json.dumps(cleaned)


# --- T6: actual CLI subprocess ------------------------------------------------

_FIXTURE = '''
import atexit, json, runpy, sys
from pathlib import Path
import trustflow.evaluation.runner as runner
import trustflow.evaluation.ablation as ablation
from trustflow.evaluation.cases import CASES_BY_ID
MARK = "REVIEWSECRET0001"
calls = []
class CustomInt(int):
    def __str__(self):
        calls.append(1)
        return "Authorization Bearer " + MARK
    def __repr__(self):
        calls.append(1)
        return "Authorization Bearer " + MARK
runner.EVAL_CASES = [CASES_BY_ID["balance_check"]]
real = runner.run_evaluation_sync
def injected(cases=None):
    report, traces = real(cases)
    for trace in traces.values():
        if trace["spans"]:
            trace["spans"][0]["detail"] = {CustomInt(7): "diagnostic"}
    return report, traces
runner.run_evaluation_sync = injected
ablation.run_ablations_sync = lambda: {"diagnostic": {CustomInt(7): "diagnostic"}}
hooks = Path(sys.argv[1])
atexit.register(lambda: hooks.write_text(json.dumps({"calls": len(calls)}), encoding="utf-8"))
sys.argv = ["trustflow.evaluation", *sys.argv[2:]]
runpy.run_module("trustflow.evaluation", run_name="__main__")
'''


def test_t6_cli_numeric_key_no_marker_anywhere(tmp_path):
    hooks = tmp_path / "hooks.json"
    fixture = tmp_path / "r4_fixture.py"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    out = tmp_path / "o"
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{SRC}"
    env["TRUSTFLOW_LLM_MODE"] = "fake"
    proc = subprocess.run(
        [sys.executable, str(fixture), str(hooks), "--out", str(out), "--ablations"],
        capture_output=True, text=True, env=env, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(hooks.read_text(encoding="utf-8"))["calls"] == 0
    assert MARK not in proc.stdout + proc.stderr
    leaked = [str(p) for p in out.rglob("*")
              if p.is_file() and MARK.encode() in p.read_bytes()]
    assert leaked == []
    for name in ("evaluation-results.json", "traces.json", "ablations.json",
                 "reproducibility-manifest.json", "repeats.json", "COMPLETE.json"):
        json.loads((out / name).read_text(encoding="utf-8"))
    assert (out / "evaluation-summary.csv").exists()
    assert (out / "evaluation-report.md").exists()
