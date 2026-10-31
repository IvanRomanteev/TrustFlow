"""End-to-end trace export derived from the authoritative audit log.

The workflow run_id is the trace_id: retries, resumes and recovery
continue the same trace because they continue the same run and the
same audit stream. Stage names map audit actors onto the pipeline
(run created -> planning -> policy/retrieval -> approval -> executor/
ledger -> verification -> persistence/recovery). Events are built
only from AuditLogger records, whose details are already redacted by
redact_mapping, and are additionally passed through redact_any so an
export can never carry credentials or approval tokens. A missing
audit stream yields an empty trace, never a fabricated one.
"""

from trustflow.core.logging import redact_any

_STAGE_BY_ACTOR = {
    "planner": "planning",
    "policy": "policy",
    "retrieval": "retrieval",
    "system": "persistence_recovery",
    "executor": "execution",
    "verifier": "verification",
    "semantic": "semantic_verification",
}


def stage_for(actor: str, action: str) -> str:
    if action == "workflow_created":
        return "run_created"
    if "approval" in action:
        return "approval"
    if actor in ("deterministic",):
        return "deterministic_verification"
    return _STAGE_BY_ACTOR.get(actor, actor or "unknown")


def build_trace(run_id: str, events: list) -> dict:
    """Normalized trace dict for one run from its audit events."""
    spans = []
    for index, event in enumerate(events):
        detail = redact_any(dict(event.detail or {}))
        spans.append({
            "schema_version": "1.0",
            "trace_id": run_id,
            "span_id": f"{run_id}:{index}",
            "parent_span_id": f"{run_id}:{index - 1}" if index else None,
            "run_id": run_id,
            "stage": stage_for(event.actor, event.action),
            "event": event.action,
            "actor": event.actor,
            "ts": event.ts.isoformat() if hasattr(event.ts, "isoformat") else str(event.ts),
            "payload_hash": event.payload_hash,
            "detail": detail,
        })
    return {"trace_id": run_id, "run_id": run_id, "spans": spans}


def render_timeline(trace: dict) -> str:
    """Human-readable per-run timeline (Markdown)."""
    lines = [f"# Trace {trace['trace_id']}", ""]
    if not trace["spans"]:
        lines.append("No audit events recorded for this run.")
    for span in trace["spans"]:
        lines.append(
            f"- `{span['ts']}` **{span['stage']}** / {span['event']} "
            f"(actor: {span['actor']}, payload_hash: `{span['payload_hash'][:12]}…`)")
    return "\n".join(lines) + "\n"
