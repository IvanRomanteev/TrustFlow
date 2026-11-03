"""M3 remediation F1-F5 regressions (independent review 695bfd8).

Crash cases run real child processes that die with os._exit after a
ledger commit / completed review, over isolated temporary databases.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from trustflow.api.app import Container, create_app
from trustflow.core.errors import ConcurrentUpdateError
from trustflow.core.settings import Settings
from trustflow.domain.models import WorkflowGoal, WorkflowRun
from trustflow.llm.provider import FakeLLMProvider
from trustflow.persistence import SQLiteRunRepository

TOKEN = "remediation-demo-token"
HEAD = {"X-Approval-Token": TOKEN}
ROOT = Path(__file__).resolve().parents[2]

CHILD = textwrap.dedent("""
    import os, sys
    mode, db = sys.argv[1], sys.argv[2]
    from fastapi.testclient import TestClient
    from trustflow.api.app import Container, create_app
    from trustflow.core.settings import Settings
    from trustflow.llm.provider import FakeLLMProvider
    token = "remediation-demo-token"
    headers = {"X-Approval-Token": token}
    c = Container(Settings(db_path=db, approval_token=token, llm_mode="fake"))
    client = TestClient(create_app(c))
    if mode in ("low", "high"):
        orig = c.registry.ledger.transfer
        def tr(*a, **k):
            result = orig(*a, **k)
            os._exit(72)
        c.registry.ledger.transfer = tr
        if mode == "low":
            client.post("/workflows", json={"title": "Transfer 100"})
        else:
            r = client.post("/workflows", json={"title": "Transfer 5000"}).json()
            client.post(f"/workflows/{r['run_id']}/approve",
                        json={"actor": "m", "plan_hash": r["plan"]["plan_hash"]},
                        headers=headers)
    elif mode == "attempt_setup":
        c.engine.semantic._llm = FakeLLMProvider(review={
            "verdict": "FAIL", "confidence": 1.0,
            "issues": ["injected"], "summary": "injected"})
        client.post("/workflows", json={"title": "Transfer 100"})
    elif mode == "attempt_crash":
        orig_review = c.engine.semantic.review
        def rev(*a, **k):
            result = orig_review(*a, **k)
            os._exit(79)
        c.engine.semantic.review = rev
        run = c.repo.list()[0]
        client.post(f"/workflows/{run.run_id}/verify-retry", headers=headers)
""")


def _box(db, **kw) -> Container:
    return Container(Settings(db_path=str(db), approval_token=TOKEN, **kw))


def _child(tmp_path: Path, mode: str, db: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "crash_child.py"
    script.write_text(CHILD, encoding="utf8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, str(script), mode, str(db)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


def test_f2_stale_detached_copy_cannot_overwrite(tmp_path):
    db = str(tmp_path / "cas.db")
    repo = SQLiteRunRepository(db)
    run = WorkflowRun(goal=WorkflowGoal(title="x"))
    repo.save(run)
    a = repo.get(run.run_id)
    b = repo.get(run.run_id)
    a.error = "first"
    repo.save(a)
    b.error = "stale"
    with pytest.raises(ConcurrentUpdateError):
        repo.save(b)
    assert SQLiteRunRepository(db).get(run.run_id).error == "first"


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_f2_concurrent_decisions_single_winner(tmp_path, decision):
    box = _box(tmp_path / "race.db")
    app = create_app(box)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as client:
        created = (await client.post(
            "/workflows", json={"title": "Transfer 5000"})).json()
        payload = {"actor": "m", "plan_hash": created["plan"]["plan_hash"]}
        entered = asyncio.Event()
        release = asyncio.Event()
        original = box.engine.executor.execute

        async def paused(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        box.engine.executor.execute = paused
        first = asyncio.create_task(client.post(
            f"/workflows/{created['run_id']}/approve",
            json=payload, headers=HEAD))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(client.post(
            f"/workflows/{created['run_id']}/{decision}",
            json=payload, headers=HEAD))
        await asyncio.sleep(0.1)
        release.set()
        responses = await asyncio.gather(first, second)
        assert sorted(r.status_code for r in responses) == [200, 409]
        granted = [e for e in box.audit.list(created["run_id"])
                   if e.action in ("approval_granted", "approval_rejected")]
        assert len(granted) == 1


def test_f1_low_risk_crash_after_commit_recovers_completed(tmp_path):
    db = tmp_path / "low.db"
    assert _child(tmp_path, "low", db).returncode == 72
    box = _box(db)
    run = box.repo.list()[0]
    assert run.status.value == "VERIFICATION_PENDING"
    assert run.plan is not None and run.intents
    client = TestClient(create_app(box))
    done = client.post(
        f"/workflows/{run.run_id}/verify-retry", headers=HEAD).json()
    assert done["status"] == "COMPLETED"
    assert len(box.registry.ledger.transactions) == 1


def test_f1_high_risk_crash_after_commit_keeps_approval(tmp_path):
    db = tmp_path / "high.db"
    assert _child(tmp_path, "high", db).returncode == 72
    box = _box(db)
    run = box.repo.list()[0]
    assert len(run.approvals) == 1 and run.approved_hash
    assert run.intents[0].status.value in ("AUTHORIZED", "COMMITTED")
    client = TestClient(create_app(box))
    rejected = client.post(
        f"/workflows/{run.run_id}/reject",
        json={"actor": "m", "plan_hash": run.plan.plan_hash}, headers=HEAD)
    assert rejected.status_code == 409
    done = client.post(
        f"/workflows/{run.run_id}/verify-retry", headers=HEAD).json()
    assert done["status"] == "COMPLETED"
    assert len(box.registry.ledger.transactions) == 1


def test_f4_semantic_attempts_survive_crash_after_review(tmp_path):
    db = tmp_path / "attempts.db"
    assert _child(tmp_path, "attempt_setup", db).returncode == 0
    assert _box(db).repo.list()[0].verification_attempts == 1
    for expected in (2, 3):
        assert _child(tmp_path, "attempt_crash", db).returncode == 79
        assert _box(db).repo.list()[0].verification_attempts == expected
    box = _box(db)
    client = TestClient(create_app(box))
    run = box.repo.list()[0]
    final = client.post(
        f"/workflows/{run.run_id}/verify-retry", headers=HEAD).json()
    assert final["status"] == "FAILED"
    assert final["verification_attempts"] == 3
    assert len(box.registry.ledger.transactions) == 1


class _Counted(FakeLLMProvider):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def propose_plan(self, *args, **kwargs):
        raw, rec = super().propose_plan(*args, **kwargs)
        rec.input_tokens, rec.output_tokens = 700, 100
        return raw, rec

    def semantic_review(self, *args, **kwargs):
        raw, rec = super().semantic_review(*args, **kwargs)
        rec.input_tokens, rec.output_tokens = 700, 100
        if self.fail:
            raw = {"verdict": "FAIL", "confidence": 1.0,
                   "issues": ["injected"], "summary": "injected"}
        return raw, rec


def test_f3_restart_preserves_usage_history_and_budget(tmp_path):
    db = tmp_path / "usage.db"
    box = _box(db, token_budget_per_workflow=3000)
    box.engine.planner._llm = _Counted()
    box.engine.semantic._llm = _Counted(fail=True)
    client = TestClient(create_app(box))
    before = client.post("/workflows", json={"title": "Transfer 100"}).json()
    assert before["status"] == "VERIFICATION_PENDING"
    assert len(before["llm_calls"]) == 2
    restarted = _box(db, token_budget_per_workflow=3000)
    restarted.engine.semantic._llm = _Counted()
    client2 = TestClient(create_app(restarted))
    after = client2.post(
        f"/workflows/{before['run_id']}/verify-retry", headers=HEAD).json()
    # Budget already spent (1600 of 3000) blocks the same call after
    # restart that the no-restart control blocks; history is intact.
    assert after["status"] == "VERIFICATION_PENDING"
    assert after["llm_calls"][:2] == before["llm_calls"]


def test_f5_deterministic_failure_after_recovery_is_bounded(tmp_path):
    db = tmp_path / "bounded.db"
    box = _box(db)
    client = TestClient(create_app(box))
    created = client.post(
        "/workflows", json={"title": "Transfer 5000"}).json()
    run = box.repo.get(created["run_id"])
    box.approvals.approve(run, "m", created["plan"]["plan_hash"])
    box.repo.save(run)  # durable EXECUTING claim, nothing executed
    restarted = _box(db)
    client2 = TestClient(create_app(restarted))
    final = {}
    for _ in range(5):
        final = client2.post(
            f"/workflows/{run.run_id}/verify-retry", headers=HEAD).json()
    assert final["status"] == "FAILED"
    assert restarted.registry.ledger.transactions == []
