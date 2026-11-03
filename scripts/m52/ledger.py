"""Independent read-only SQLite oracles and an exclusively owned fresh seed."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

SEED_ID = "m52-synthetic-v1"
BALANCES = {"acc_alice": 25000.0, "acc_bob": 1200.0, "acc_ops": 10000.0}
TOTAL = sum(BALANCES.values())
TABLE_COLUMNS = {
    "runs": {"run_id", "status", "version", "run_json", "updated_at"},
    "ledger_accounts": {"account_id", "owner", "balance", "currency"},
    "ledger_transactions": {
        "seq", "tx_id", "idempotency_key", "from_account", "to_account", "amount", "currency",
    },
    "audit_events": {"id", "run_id", "ts", "actor", "action", "payload_hash", "detail_json"},
}


def seed_owned(stack) -> None:
    """Initialize ONLY a fresh file in the accepted lifecycle's verified bind directory.

    This is the sole demo write to ledger rows: a documented starting dataset,
    before the backend starts. Reset means a new invocation, never DELETE/UPDATE
    of an existing database. Application transactions are never edited.
    """
    from trustflow.persistence.ledger import PersistentLedger

    stack._validate_workspace()
    path = stack.bind_dir / "trustflow.db"
    if path.exists() or path.is_symlink():
        raise AssertionError("seed refuses existing SQLite file")
    if set(stack.bind_dir.iterdir()) != {stack.marker_file}:
        raise AssertionError("seed refuses nonempty or foreign storage")
    ledger = PersistentLedger(str(path))
    conn = ledger._conn
    try:
        assert_schema(conn)
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM ledger_transactions").fetchone()[0] == 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("UPDATE ledger_accounts SET balance=? WHERE account_id=?",
                             [(v, k) for k, v in BALANCES.items()])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    stack._validate_workspace()
    snap = snapshot(path)
    assert snap["balances"] == BALANCES and snap["total"] == TOTAL
    assert not snap["transactions"] and not snap["runs"] and not snap["audit"]


def assert_schema(conn: sqlite3.Connection) -> None:
    for table, expected in TABLE_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert actual == expected, (table, actual, expected)


def snapshot(path: Path) -> dict:
    """Read one SQLite transaction through its live WAL, never copy db-only bytes."""
    if not path.is_file():
        raise AssertionError(f"SQLite evidence missing: {path}")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        assert_schema(conn)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        accounts = [dict(r) for r in conn.execute(
            "SELECT * FROM ledger_accounts ORDER BY account_id")]
        transactions = [dict(r) for r in conn.execute(
            "SELECT * FROM ledger_transactions ORDER BY seq")]
        runs = {r["run_id"]: {"status": r["status"], "version": r["version"],
                              "run": json.loads(r["run_json"])}
                for r in conn.execute("SELECT * FROM runs ORDER BY run_id")}
        audit = [{**dict(r), "detail": json.loads(r["detail_json"])}
                 for r in conn.execute("SELECT * FROM audit_events ORDER BY id")]
        conn.rollback()
    balances = {a["account_id"]: a["balance"] for a in accounts}
    return {"accounts": accounts, "balances": balances, "total": sum(balances.values()),
            "transactions": transactions, "runs": runs, "audit": audit}


def plan_hash(plan: dict) -> str:
    """Independent implementation of the documented approval canonical format."""
    payload = {"goal_title": plan["goal_title"], "version": plan["version"], "steps": [
        {k: s[k] for k in ("step_id", "tool_name", "intent_id", "operation_id", "args")}
        for s in plan["steps"]]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def transactions_for(snap: dict, run: dict) -> list[dict]:
    keys = {i["intent_id"] for i in run["intents"]}
    return [t for t in snap["transactions"] if t["idempotency_key"] in keys]


def assert_integrity(snap: dict) -> None:
    txs = snap["transactions"]
    assert snap["total"] == TOTAL, "ledger conservation"
    assert all(v >= 0 for v in snap["balances"].values()), "negative balance"
    assert len({t["tx_id"] for t in txs}) == len(txs), "duplicate transaction identity"
    assert len({t["idempotency_key"] for t in txs}) == len(txs), "duplicate operation effect"
    expected = dict(BALANCES)
    for t in txs:
        assert t["amount"] > 0 and t["currency"] == "USD"
        expected[t["from_account"]] -= t["amount"]
        expected[t["to_account"]] += t["amount"]
    assert snap["balances"] == expected, "balances disagree with independent ledger replay"
    for e in snap["audit"]:
        digest = hashlib.sha256(json.dumps(e["detail"], sort_keys=True, default=str).encode())
        assert digest.hexdigest() == e["payload_hash"], "audit payload digest mismatch"
    # Every committed operation must have one authoritative authorized intent and
    # a persisted snapshot with an independently recomputed exact approval hash.
    for t in txs:
        owners = [r["run"] for r in snap["runs"].values()
                  if any(i["intent_id"] == t["idempotency_key"] for i in r["run"]["intents"])]
        assert len(owners) == 1, "ledger operation has no unique workflow"
        run = owners[0]
        intent = next(i for i in run["intents"] if i["intent_id"] == t["idempotency_key"])
        assert intent["authorized"] and intent["ever_authorized"]
        assert all(intent["authorized_payload"][k] == t[k]
                   for k in ("from_account", "to_account", "amount", "currency"))
        if any(s["risk"] == "HIGH" for s in run["plan"]["steps"]):
            plan = run["approved_plan_snapshot"]
            assert plan and plan_hash(plan) == run["approved_hash"] == plan_hash(run["plan"])
            decisions = [a for a in run["approvals"] if a["decision"] == "APPROVED"]
            assert len(decisions) == 1 and decisions[0]["plan_hash"] == run["approved_hash"]
            grants = [e for e in snap["audit"]
                      if e["run_id"] == run["run_id"] and e["action"] == "approval_granted"]
            assert len(grants) == 1 and grants[0]["detail"]["plan_hash"] == run["approved_hash"]


def assert_api_matches(snap: dict, run: dict, events: list[dict]) -> None:
    stored = snap["runs"][run["run_id"]]
    assert stored["status"] == run["status"]
    assert stored["run"] == run, "authoritative GET disagrees with SQLite run_json"
    actual = [{k: e[k] for k in ("run_id", "ts", "actor", "action", "payload_hash", "detail")}
              for e in snap["audit"] if e["run_id"] == run["run_id"]]
    # SQLiteAuditLogger stores datetime.isoformat() (+00:00); the Pydantic API
    # emits UTC as Z. Compare the same instants, preserving ordering/content.
    def comparable(event):
        return {**event, "ts": datetime.fromisoformat(event["ts"])}

    assert [comparable(e) for e in actual] == [comparable(e) for e in events], (
        "API audit disagrees with independent SQLite audit")
