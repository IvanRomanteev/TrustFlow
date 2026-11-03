"""M3 persistence: durability, idempotency, atomicity, recovery.

Each "restart" builds brand-new repository/ledger/audit objects over
the same database file, which is the process-restart boundary.
"""

import threading

import pytest

from trustflow.core.errors import ConcurrentUpdateError, ConflictingOperationError
from trustflow.domain.models import RunStatus, WorkflowGoal, WorkflowRun
from trustflow.domain.state_machine import transition
from trustflow.persistence import (
    PersistentLedger,
    SQLiteAuditLogger,
    SQLiteRunRepository,
    recover_interrupted_runs,
)
from trustflow.persistence.db import connect, migrate


def _run(status: RunStatus = RunStatus.CREATED) -> WorkflowRun:
    run = WorkflowRun(goal=WorkflowGoal(title="Transfer 100",
                                        description="transfer 100 to bob"))
    run.status = status
    return run


def test_run_round_trip_and_restart(tmp_path):
    db = str(tmp_path / "tf.db")
    repo = SQLiteRunRepository(db)
    run = _run(RunStatus.AWAITING_APPROVAL)
    repo.save(run)
    assert SQLiteRunRepository(db).get(run.run_id).status == RunStatus.AWAITING_APPROVAL
    assert [r.run_id for r in SQLiteRunRepository(db).list()] == [run.run_id]


def test_optimistic_concurrency_blocks_second_writer(tmp_path):
    db = str(tmp_path / "tf.db")
    repo = SQLiteRunRepository(db)
    run = _run()
    repo.save(run)
    first = SQLiteRunRepository(db)
    second = SQLiteRunRepository(db)
    a = first.get(run.run_id)
    b = second.get(run.run_id)
    a.error = "first writer"
    first.save(a)
    b.error = "stale writer"
    with pytest.raises(ConcurrentUpdateError):
        second.save(b)
    assert SQLiteRunRepository(db).get(run.run_id).error == "first writer"


def test_ledger_transfer_durable_and_idempotent_after_restart(tmp_path):
    db = str(tmp_path / "tf.db")
    ledger = PersistentLedger(db)
    tx = ledger.transfer("acc_alice", "acc_bob", 100.0, "op_1")
    assert tx.tx_id == "tx_0001"
    restarted = PersistentLedger(db)
    assert restarted.committed("op_1").tx_id == "tx_0001"
    again = restarted.transfer("acc_alice", "acc_bob", 100.0, "op_1")
    assert again.tx_id == "tx_0001"
    assert len(restarted.transactions) == 1
    assert restarted.get_balance("acc_alice").balance == 4900.0
    assert restarted.total_balance() == 16200.0


def test_ledger_conflicting_key_rejected_after_restart(tmp_path):
    db = str(tmp_path / "tf.db")
    PersistentLedger(db).transfer("acc_alice", "acc_bob", 100.0, "op_1")
    with pytest.raises(ConflictingOperationError):
        PersistentLedger(db).transfer("acc_alice", "acc_bob", 101.0, "op_1")
    assert len(PersistentLedger(db).transactions) == 1


def test_ledger_failed_transfer_is_atomic(tmp_path):
    db = str(tmp_path / "tf.db")
    ledger = PersistentLedger(db)
    with pytest.raises(ValueError, match="Insufficient"):
        ledger.transfer("acc_bob", "acc_alice", 99999.0, "op_big")
    restarted = PersistentLedger(db)
    assert restarted.transactions == []
    assert restarted.get_balance("acc_bob").balance == 1200.0


def test_ledger_concurrent_same_key_commits_once(tmp_path):
    db = str(tmp_path / "tf.db")
    PersistentLedger(db)  # create schema before threads race
    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            tx = PersistentLedger(db).transfer(
                "acc_alice", "acc_bob", 50.0, "op_race")
            results.append(tx.tx_id)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(set(results)) == 1
    assert len(PersistentLedger(db).transactions) == 1


def test_audit_append_only_survives_restart(tmp_path):
    db = str(tmp_path / "tf.db")
    audit = SQLiteAuditLogger(db)
    audit.log("run_1", "mgr", "approval_granted", {"plan_hash": "a" * 64})
    audit.log("run_1", "system", "restart_recovered", {})
    events = SQLiteAuditLogger(db).list("run_1")
    assert [e.action for e in events] == ["approval_granted", "restart_recovered"]
    assert events[0].detail == {"plan_hash": "a" * 64}
    assert events[0].payload_hash


def test_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "tf.db")
    conn = connect(db)
    assert migrate(conn) == []
    versions = [r[0] for r in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [1]


def test_recovery_moves_stranded_runs_without_touching_approval(tmp_path):
    db = str(tmp_path / "tf.db")
    repo = SQLiteRunRepository(db)
    stranded = _run()
    transition(stranded, RunStatus.PLANNING)
    stranded.status = RunStatus.EXECUTING
    repo.save(stranded)
    waiting = _run(RunStatus.AWAITING_APPROVAL)
    repo.save(waiting)
    recovered = recover_interrupted_runs(
        SQLiteRunRepository(db), SQLiteAuditLogger(db))
    assert recovered == [stranded.run_id]
    after = SQLiteRunRepository(db)
    assert after.get(stranded.run_id).status == RunStatus.VERIFICATION_PENDING
    assert "no re-execution" in (after.get(stranded.run_id).error or "")
    assert after.get(waiting.run_id).status == RunStatus.AWAITING_APPROVAL
    actions = [e.action for e in SQLiteAuditLogger(db).list(stranded.run_id)]
    assert actions == ["restart_recovered"]
