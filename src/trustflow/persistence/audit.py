"""Durable append-only audit log (M3): same API as ``AuditLogger``.

Events are only ever inserted and read back in insertion order; there
is no update or delete path. Payload hashing/redaction is unchanged.
"""

import hashlib
import json
import threading

from trustflow.audit.logger import AuditEvent, AuditLogger
from trustflow.core.logging import redact_mapping
from trustflow.persistence.db import connect


class SQLiteAuditLogger(AuditLogger):
    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)
        self._lock = threading.Lock()

    def log(self, run_id: str, actor: str, action: str,
            detail: dict | None = None) -> AuditEvent:
        safe = redact_mapping(detail or {})
        digest = hashlib.sha256(
            json.dumps(safe, sort_keys=True, default=str).encode()
        ).hexdigest()
        event = AuditEvent(run_id=run_id, actor=actor, action=action,
                           payload_hash=digest, detail=safe)
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_events(run_id, ts, actor, action,"
                " payload_hash, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, event.ts.isoformat(), actor, action, digest,
                 json.dumps(safe, sort_keys=True, default=str)))
        return event

    def list(self, run_id: str) -> list[AuditEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, ts, actor, action, payload_hash, detail_json"
                " FROM audit_events WHERE run_id = ? ORDER BY id",
                (run_id,)).fetchall()
        return [
            AuditEvent(run_id=row["run_id"], ts=row["ts"], actor=row["actor"],
                       action=row["action"], payload_hash=row["payload_hash"],
                       detail=json.loads(row["detail_json"]))
            for row in rows
        ]
