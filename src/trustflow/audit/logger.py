"""AuditLogger: append-only, hashed, secret-free audit events."""

import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from trustflow.core.logging import redact_mapping


class AuditEvent(BaseModel):
    run_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str
    action: str
    payload_hash: str = ""
    detail: dict = {}


class AuditLogger:
    def __init__(self) -> None:
        self._events: dict[str, list[AuditEvent]] = {}

    def log(self, run_id: str, actor: str, action: str, detail: dict | None = None) -> AuditEvent:
        safe = redact_mapping(detail or {})
        digest = hashlib.sha256(
            json.dumps(safe, sort_keys=True, default=str).encode()
        ).hexdigest()
        event = AuditEvent(
            run_id=run_id, actor=actor, action=action, payload_hash=digest, detail=safe
        )
        self._events.setdefault(run_id, []).append(event)
        return event

    def list(self, run_id: str) -> list[AuditEvent]:
        return list(self._events.get(run_id, []))
