"""M3 remediation round 2: N1 (verification-only reconcile) regressions.

The dead-worker case is a real child process held before the ledger
call and killed with os._exit(81), exactly like the independent
reproducer; recovery runs in a brand-new Container over the same DB.
"""

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trustflow.api.app import Container, create_app
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "remediation2-demo-token"
HEAD = {"X-Approval-Token": TOKEN}

CHILD = textwrap.dedent("""
    import asyncio, os, sys, threading
    db = sys.argv[1]
    from trustflow.api.app import Container
    from trustflow.core.settings import Settings
    from trustflow.domain.models import WorkflowGoal
    from trustflow.llm.provider import FakeLLMProvider
    c = Container(Settings(db_path=db, llm_mode="fake",
                           step_timeout_seconds=0.03))
    c.engine.planner._llm = FakeLLMProvider(scripted=[{
        "tool_name": "simulate_transfer", "from_account": "acc_alice",
        "to_account": "acc_bob", "amount": 100, "currency": "USD"}])
    entered = threading.Event()
    release = threading.Event()
    orig = c.registry.ledger.transfer
    def slow(*a, **kw):
        entered.set()
        release.wait()
        return orig(*a, **kw)
    c.registry.ledger.transfer = slow
    async def run():
        run = await c.engine.create_workflow(WorkflowGoal(title="Transfer 100"))
        assert entered.is_set() and str(run.status) == "EXECUTION_UNCERTAIN"
        assert not c.registry.ledger.transactions
        os._exit(81)
    asyncio.run(run())
""")


def _box(db, **kw) -> Container:
    return Container(Settings(db_path=str(db), approval_token=TOKEN, **kw))


def test_n1_dead_worker_reconcile_never_replays_mutation(tmp_path):
    script = tmp_path / "lost_worker_child.py"
    script.write_text(CHILD, encoding="utf8")
    db = tmp_path / "lost.db"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, str(script), str(db)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 81, proc.stdout + proc.stderr

    box = _box(db)
    before = box.repo.list()[0]
    assert before.status == RunStatus.EXECUTION_UNCERTAIN
    assert box.registry.ledger.transactions == []
    calls: list[str] = []
    plans: list[int] = []
    invoke = box.registry.invoke
    plan = box.engine.planner.create_plan

    def trace(tool, *a, **kw):
        calls.append(tool)
        return invoke(tool, *a, **kw)

    def trace_plan(*a, **kw):
        plans.append(1)
        return plan(*a, **kw)

    box.registry.invoke = trace
    box.engine.planner.create_plan = trace_plan
    client = TestClient(create_app(box))
    response = client.post(f"/workflows/{before.run_id}/reconcile", headers=HEAD)
    assert response.status_code == 200
    after = box.repo.get(before.run_id)
    assert after.status == RunStatus.FAILED
    assert box.registry.ledger.transactions == []
    assert "simulate_transfer" not in calls
    assert plans == []
    assert not any(e.action == "plan_revised"
                   for e in box.audit.list(before.run_id))
    # Repeated recovery stays terminal, still no replay.
    again = client.post(f"/workflows/{before.run_id}/reconcile", headers=HEAD)
    assert again.status_code == 200
    assert box.repo.get(before.run_id).status == RunStatus.FAILED
    assert box.registry.ledger.transactions == [] and plans == []


@pytest.mark.asyncio
async def test_n1_live_worker_reconcile_waits_then_completes_once(tmp_path):
    box = _box(tmp_path / "live.db", step_timeout_seconds=0.05)
    box.engine.planner._llm = FakeLLMProvider(scripted=[{
        "tool_name": "simulate_transfer", "from_account": "acc_alice",
        "to_account": "acc_bob", "amount": 100, "currency": "USD"}])
    entered = threading.Event()
    release = threading.Event()
    orig = box.registry.ledger.transfer

    def slow(*a, **kw):
        entered.set()
        release.wait(timeout=10)
        return orig(*a, **kw)

    box.registry.ledger.transfer = slow
    plans: list[int] = []
    plan = box.engine.planner.create_plan

    def trace_plan(*a, **kw):
        plans.append(1)
        return plan(*a, **kw)

    box.engine.planner.create_plan = trace_plan
    run = await box.engine.create_workflow(WorkflowGoal(title="Transfer 100"))
    assert entered.is_set() and run.status == RunStatus.EXECUTION_UNCERTAIN
    # Live worker: repeated reconcile launches no replacement.
    for _ in range(3):
        run = await box.engine.reconcile(box.repo.get(run.run_id))
        assert run.status == RunStatus.EXECUTION_UNCERTAIN
    assert box.registry.ledger.transactions == []
    release.set()
    time.sleep(0.3)
    run = await box.engine.reconcile(box.repo.get(run.run_id))
    assert run.status == RunStatus.COMPLETED
    assert len(box.registry.ledger.transactions) == 1
    assert plans == [1]  # only the initial plan, never a revision
