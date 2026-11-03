"""Offline contract tests for M5.2's explicit test-only fault mechanism."""

import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from scripts.m52.fault_backend import build_app
from scripts.m52.recovery import LATE_WORKER_GOAL, MISSING_EVIDENCE_GOAL

from trustflow.core.settings import Settings

TOKEN = "offline-m52-placeholder"
HEADERS = {"X-Approval-Token": TOKEN}


def _owned(tmp_path):
    run_id = uuid.uuid4().hex
    storage = tmp_path / "data"
    storage.mkdir()
    (storage / f".trustflow-owned-{run_id}").write_text(run_id, encoding="utf-8")
    settings = Settings(_env_file=None, db_path=str(storage / "trustflow.db"),
                        llm_mode="fake", approval_token=TOKEN)
    return settings, run_id, storage


def test_m52_factory_rejects_unowned_storage_and_live_provider(tmp_path):
    settings, run_id, storage = _owned(tmp_path)
    marker = storage / f".trustflow-owned-{run_id}"
    marker.write_text("foreign", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        build_app(settings, run_id, storage, tmp_path / "journal")
    assert not (storage / "trustflow.db").exists()
    marker.write_text(run_id, encoding="utf-8")
    settings.llm_mode = "live"
    with pytest.raises(RuntimeError, match="fake mode"):
        build_app(settings, run_id, storage, tmp_path / "journal")
    assert not (storage / "trustflow.db").exists()


def test_m52_late_worker_reconcile_and_retry_never_reinvoke_mutation(tmp_path):
    settings, run_id, storage = _owned(tmp_path)
    app = build_app(settings, run_id, storage, tmp_path / "journal")
    box = app.state.container
    with TestClient(app) as client:
        assert client.post("/__m52/arm", json={"mode": "late_commit"}).status_code == 401
        assert client.get("/__m52/status", headers={
            "X-Approval-Token": "wrong"}).status_code == 403
        response = client.post("/__m52/arm", json={
            "mode": "late_commit", "semantic_failures": 1}, headers=HEADERS)
        assert response.status_code == 200
        created = client.post("/workflows", json=LATE_WORKER_GOAL)
        assert created.status_code == 201
        run = created.json()
        assert run["status"] == "EXECUTION_UNCERTAIN"
        assert box.registry.ledger.transactions == []
        path = f"/workflows/{run['run_id']}"
        held = client.post(path + "/reconcile", headers=HEADERS)
        assert held.json()["status"] == "EXECUTION_UNCERTAIN"
        calls = [event for event in app.state.m52_harness.journal.read()
                 if event["event"] == "tool_invoked" and event["tool"] == "simulate_transfer"]
        assert len(calls) == 1
        client.post("/__m52/release", headers=HEADERS)
        # Thread synchronization waits for a genuine tool completion checkpoint.
        # No guessed delay is used to manufacture a late-commit outcome.
        with ThreadPoolExecutor(max_workers=1) as pool:
            def await_worker():
                import time
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if "worker_returned" in app.state.m52_harness.status()["checkpoints"]:
                        return True
                    time.sleep(0.01)
                return False
            assert pool.submit(await_worker).result(timeout=12)
        txs = box.registry.ledger.transactions
        assert len(txs) == 1
        reconciled = client.post(path + "/reconcile", headers=HEADERS)
        assert reconciled.json()["status"] == "VERIFICATION_PENDING"
        retried = client.post(path + "/verify-retry", headers=HEADERS)
        assert retried.json()["status"] == "COMPLETED"
        assert box.registry.ledger.transactions == txs
        after_calls = [event for event in app.state.m52_harness.journal.read()
                       if event["event"] == "tool_invoked"
                       and event["tool"] == "simulate_transfer"]
        assert after_calls == calls


def test_m52_missing_evidence_uses_real_retrieval_failure(tmp_path):
    settings, run_id, storage = _owned(tmp_path)
    app = build_app(settings, run_id, storage, tmp_path / "journal")
    with TestClient(app) as client:
        response = client.post("/workflows", json=MISSING_EVIDENCE_GOAL)
        assert response.status_code == 201
        run = response.json()
        assert run["status"] == "FAILED"
        assert "retrieval_unavailable" in run["error"]
        assert run["retrieval"] == [] and run["plan"] is None
        assert app.state.container.registry.ledger.transactions == []
        audit = client.get(f"/workflows/{run['run_id']}/audit").json()["events"]
        assert any(event["action"] == "mandatory_evidence_invalid" for event in audit)


def test_m52_postcommit_process_kill_rebuilds_from_ledger_without_transfer(tmp_path):
    settings, run_id, storage = _owned(tmp_path)
    journal = tmp_path / "journal"
    root = Path(__file__).resolve().parents[2]
    child = tmp_path / "crash_child.py"
    child.write_text('''
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from scripts.m52.fault_backend import build_app
from trustflow.core.settings import Settings
storage, run_id, journal = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
settings = Settings(_env_file=None, db_path=str(storage / "trustflow.db"),
                    llm_mode="fake", approval_token="offline-m52-placeholder")
app = build_app(settings, run_id, storage, journal)
with TestClient(app) as client:
    headers = {"X-Approval-Token": "offline-m52-placeholder"}
    run = client.post("/workflows", json={"title": "Transfer 5000"}).json()
    assert run["status"] == "AWAITING_APPROVAL"
    client.post("/__m52/arm", json={"mode": "after_commit"}, headers=headers)
    client.post(f"/workflows/{run['run_id']}/approve", headers=headers,
                json={"actor": "offline", "plan_hash": run["plan"]["plan_hash"]})
''', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
           "TRUSTFLOW_LLM_MODE": "fake", "MODEL_API_KEY": "offline-fake-placeholder"}
    process = subprocess.Popen([sys.executable, str(child), str(storage), run_id, str(journal)],
                               cwd=root, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                records = [json.loads(line) for line in (journal / "events.jsonl").read_text(
                    encoding="utf-8").splitlines()]
            except (OSError, ValueError):
                records = []
            if any(record.get("name") == "after_commit" for record in records):
                break
            assert process.poll() is None, process.communicate(timeout=2)
            assert time.monotonic() < deadline, "Actual post-commit checkpoint not reached"
            time.sleep(0.02)
        process.kill()
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert process.returncode != 0
    app = build_app(settings, run_id, storage, journal)
    box = app.state.container
    run = box.repo.list()[0]
    assert str(run.status) == "VERIFICATION_PENDING"
    assert run.approved_hash == run.plan.plan_hash == run.approved_plan_snapshot.plan_hash
    txs = box.registry.ledger.transactions
    assert len(txs) == 1 and txs[0].amount == 5000
    assert any(result.data.get("recovered_after_restart") for result in run.step_results)
    calls = [event for event in app.state.m52_harness.journal.read()
             if event["event"] == "tool_invoked" and event["tool"] == "simulate_transfer"]
    assert len(calls) == 1
    with TestClient(app) as client:
        response = client.post(f"/workflows/{run.run_id}/verify-retry", headers=HEADERS)
        assert response.json()["status"] == "COMPLETED"
        assert box.registry.ledger.transactions == txs
    after_calls = [event for event in app.state.m52_harness.journal.read()
                   if event["event"] == "tool_invoked" and event["tool"] == "simulate_transfer"]
    assert after_calls == calls
