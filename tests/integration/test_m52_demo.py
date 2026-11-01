"""Maintained real Chromium/Docker M5.2 acceptance exercise (expensive opt-in).

PowerShell:
    $env:TRUSTFLOW_M52_DEMO_TESTS="1"
    python -m pytest tests/integration/test_m52_demo.py -v

Both invocations must execute all six scenarios. Missing Docker, Chromium or
evidence is a failure after opt-in, never a silently successful skip.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from scripts.m52.infrastructure import TOKEN, offline_environment

pytestmark = pytest.mark.skipif(
    os.environ.get("TRUSTFLOW_M52_DEMO_TESTS") != "1",
    reason="opt-in M5.2 real-browser gate: set TRUSTFLOW_M52_DEMO_TESTS=1",
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ["S1", "S2", "S3", "S4", "S5", "S6"]
SCREENSHOTS = {
    "S1-low-risk-verified", "S2-awaiting-exact-hash", "S2-approved-verified",
    "S2-authoritative-refresh", "S2-concurrent-one-winner", "S3-rejected-authoritative",
    "S4-uncertain-fault-injected", "S4-verification-pending", "S4-recovered-no-duplicate",
    "S5-missing-mandatory-evidence", "S6-before-crash-approval",
    "S6-after-sigkill-recovered-pending", "S6-durable-ui-reload", "S6-after-graceful-restart",
}


def _read(directory: Path, name: str):
    return json.loads((directory / name).read_text(encoding="utf-8"))


def _execute(output: Path, *, previous: Path | None = None) -> Path:
    argv = [sys.executable, str(ROOT / "scripts/run_m52_demo.py"), "--out", str(output)]
    if previous is not None:
        argv += ["--compare", str(previous)]
    env = offline_environment()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=1200)
    assert result.returncode == 0, (
        f"M5.2 runner failed with exit {result.returncode}; retained evidence: {output}\n"
        f"{result.stdout[-10000:]}\n{result.stderr[-5000:]}")
    attempts = list(output.iterdir())
    assert len(attempts) == 1 and attempts[0].is_dir()
    directory = attempts[0]
    assert (directory / "COMPLETE.json").is_file()
    assert not (directory / "FAILED_ATTEMPT.json").exists()
    return directory


def _assert_evidence(directory: Path) -> dict:
    complete = _read(directory, "COMPLETE.json")
    manifest = _read(directory, "manifest.json")
    assert complete["verdict"] == manifest["verdict"] == "PASS"
    assert complete["manifest_sha256"] == hashlib.sha256(
        (directory / "manifest.json").read_bytes()).hexdigest()
    assert manifest["provider"] == "FakeLLMProvider" and manifest["llm_mode"] == "fake"
    assert manifest["scenario_ids"] == SCENARIOS
    assert manifest["counts"] == {"passed": 6, "failed": 0, "not_run": 0, "skipped": 0}
    assert manifest["cleanup"]["passed"] is True
    assert manifest["preservation"]["unchanged"] is True
    results = _read(directory, "scenario-results.json")
    assert [scenario["id"] for scenario in results] == SCENARIOS
    assert all(scenario["verdict"] == "PASS" and scenario["assertions"]
               and all(check["passed"] for check in scenario["assertions"])
               for scenario in results)
    assert results[1]["concurrent_statuses"] == [200, 409]
    assert results[3]["fault_injected"] is True
    assert results[5]["interruption"] == "actual owned-container SIGKILL after SQLite COMMIT"
    for relative, expected in manifest["artifact_hashes"].items():
        assert hashlib.sha256((directory / relative).read_bytes()).hexdigest() == expected

    browser = _read(directory, "browser-log.json")
    assert browser["chromium_version"] != "not-started"
    browser_checks = {check["name"] for check in browser["assertions"] if check["passed"]}
    assert {
        "real_chromium_frontend_connected_to_fake_sqlite_backend", "ui_approve", "ui_reject",
        "ui_run_selection", "ui_high_risk_exact_plan_hash_inspection",
        "ui_reload_authoritative_persisted_state", "ui_reconcile", "ui_verify-retry",
        "no_unexpected_browser_page_or_network_errors",
    } <= browser_checks
    responses = [event for event in browser["events"] if event["kind"] == "response"]
    assert any(event["method"] == "POST" and event["status"] == 201
               and urlsplit(event["url"]).path == "/workflows" for event in responses)
    for action in ("approve", "reject", "reconcile", "verify-retry"):
        assert any(event["method"] == "POST" and event["status"] == 200
                   and urlsplit(event["url"]).path.endswith("/" + action)
                   for event in responses), action
    screenshots = {Path(name).stem for name in browser["screenshots"]}
    assert SCREENSHOTS <= screenshots
    for relative in browser["screenshots"]:
        assert (directory / relative).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    before = _read(directory, "ledger-before.json")
    after = _read(directory, "ledger-after.json")
    assert before["balances"] == {"acc_alice": 25000.0, "acc_bob": 1200.0, "acc_ops": 10000.0}
    assert before["transactions"] == [] and before["runs"] == {} and before["audit"] == []
    assert before["total"] == after["total"] == 36200.0
    assert sorted(row["amount"] for row in after["transactions"]) == [25.0, 1500.0, 5000.0, 5000.0]
    assert len({row["tx_id"] for row in after["transactions"]}) == 4
    assert len({row["idempotency_key"] for row in after["transactions"]}) == 4
    assert after["balances"] == {"acc_alice": 13475.0, "acc_bob": 12725.0, "acc_ops": 10000.0}
    assert _read(directory, "preservation-before.json") == _read(
        directory, "preservation-after.json")

    commands = [json.loads(line) for line in (directory / "commands.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert any(command["argv"][:4] == ["docker", "kill", "--signal", "KILL"]
               and command["exit_code"] == 0 for command in commands)
    assert any(command["argv"][:2] == ["docker", "start"]
               and command["exit_code"] == 0 for command in commands)
    journal = _read(directory, "fault-journal.json")["events"]
    mutations = [event for event in journal if event["event"] == "tool_invoked"
                 and event["tool"] == "simulate_transfer"]
    assert len(mutations) == 4 and len({event["key"] for event in mutations}) == 4
    assert sum(event["event"] == "process_started" for event in journal) == 3
    for file in directory.rglob("*"):
        if file.is_file() and file.suffix in {".json", ".jsonl", ".md", ".yml"}:
            assert TOKEN not in file.read_text(encoding="utf-8"), file
    return manifest


def test_m52_two_complete_real_browser_runs_are_reproducible(tmp_path):
    first = _execute(tmp_path / "first")
    first_manifest = _assert_evidence(first)
    second = _execute(tmp_path / "second", previous=first)
    second_manifest = _assert_evidence(second)
    assert first_manifest["invocation_id"] != second_manifest["invocation_id"]
    assert first_manifest["project"] != second_manifest["project"]
    assert first_manifest["normalized_digest"] == second_manifest["normalized_digest"]
    assert _read(second, "comparison.json")["equivalent"] is True
    assert _read(first, "normalized-results.json") == _read(second, "normalized-results.json")
    assert set(_read(first, "ledger-after.json")["runs"]).isdisjoint(
        _read(second, "ledger-after.json")["runs"])
