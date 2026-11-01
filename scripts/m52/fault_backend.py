"""M5.2 test-only FastAPI entrypoint; never imported by the production image.

The hooks wrap real tool/provider contracts. They never edit persisted workflow
state or ledger rows. A marker-bound fake-only factory is deliberately required.
"""

import hmac
import importlib
import json
import os
import re
import threading
import types
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field

from trustflow.core.settings import Settings
from trustflow.llm.provider import FakeLLMProvider
from trustflow.tools.registry import ToolRegistry


class Journal:
    """Append-only container-local observations, durable across kill/start."""

    def __init__(self, directory: Path, run_id: str) -> None:
        self.directory = directory
        self.lock = threading.RLock()
        marker = directory / "owner"
        if directory.exists():
            if directory.is_symlink() or marker.is_symlink() \
                    or marker.read_text(encoding="utf-8") != run_id:
                raise RuntimeError("M5.2 journal ownership mismatch")
        else:
            directory.mkdir(parents=True, exist_ok=False)
            marker.write_text(run_id, encoding="utf-8")
        self.path = directory / "events.jsonl"
        if self.path.is_symlink():
            raise RuntimeError("M5.2 journal must be a regular owned file")
        self.events = [json.loads(line) for line in self.path.read_text(
            encoding="utf-8").splitlines()] if self.path.exists() else []

    def append(self, event: str, **detail) -> dict:
        with self.lock:
            record = {"seq": len(self.events) + 1, "event": event, **detail}
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.events.append(record)
            return record

    def read(self) -> list[dict]:
        with self.lock:
            return [dict(event) for event in self.events]


class FaultHarness:
    def __init__(self, journal: Journal, settings: Settings) -> None:
        self.journal = journal
        self.settings = settings
        self.lock = threading.RLock()
        self.release_event = threading.Event()
        self.mode: str | None = None
        self.worker_active = False
        self.semantic_failures = 0
        self.checkpoints: dict[str, dict] = {}

    def checkpoint(self, name: str, **detail) -> None:
        with self.lock:
            self.checkpoints[name] = detail
            self.journal.append("checkpoint", name=name, **detail)

    def arm(self, mode: str, semantic_failures: int) -> None:
        with self.lock:
            if mode not in {"late_commit", "after_commit"}:
                raise HTTPException(422, "Unknown M5.2 fault mode")
            if self.worker_active or self.mode is not None:
                raise HTTPException(409, "An M5.2 checkpoint is already armed or active")
            self.mode = mode
            self.semantic_failures = semantic_failures
            self.release_event.clear()
            self.checkpoints = {}
            # Late commit exercises the maintained real timeout contract.
            # The crash checkpoint instead gives the controller a bounded,
            # observable 120-second window before an executor timeout.
            self.settings.step_timeout_seconds = 0.15 if mode == "late_commit" else 120.0
            self.checkpoint("armed", mode=mode)

    def invoke(self, invoke, name: str, raw_args: dict, key: str):
        self.journal.append("tool_invoked", tool=name, key=key)
        with self.lock:
            mode = self.mode if name == "simulate_transfer" else None
            if mode:
                self.mode = None  # only this exact invocation claims the hook
                self.worker_active = True
        if not mode:
            return invoke(name, raw_args, key)
        try:
            self.checkpoint("worker_entered", mode=mode, key=key)
            if mode == "late_commit":
                if not self.release_event.wait(timeout=120):
                    self.checkpoint("hook_timeout", mode=mode, key=key)
                    raise RuntimeError("M5.2 pre-commit checkpoint was not released")
            outcome = invoke(name, raw_args, key)
            if not outcome.ok:
                self.checkpoint("hook_tool_failure", mode=mode, key=key)
                return outcome
            self.checkpoint("ledger_committed", mode=mode, key=key,
                            tx_id=outcome.data.get("tx_id"))
            if mode == "after_commit":
                # Actual SQLite COMMIT has happened; the tool result has not
                # returned to Executor, so no StepResult can be persisted yet.
                self.checkpoint("after_commit", key=key,
                                tx_id=outcome.data.get("tx_id"))
                if not self.release_event.wait(timeout=120):
                    self.checkpoint("hook_timeout", mode=mode, key=key)
                    raise RuntimeError("M5.2 crash checkpoint was not interrupted")
            self.checkpoint("worker_returned", mode=mode, key=key)
            return outcome
        finally:
            with self.lock:
                self.worker_active = False

    def status(self) -> dict:
        with self.lock:
            return {
                "fault_injected": True,
                "mode": self.mode,
                "worker_active": self.worker_active,
                "semantic_failures_remaining": self.semantic_failures,
                "checkpoints": dict(self.checkpoints),
                "events": self.journal.read(),
            }


class FaultingFakeProvider(FakeLLMProvider):
    """One maintained FakeLLMProvider FAIL, then ordinary fake evidence review."""

    def __init__(self, harness: FaultHarness) -> None:
        super().__init__()
        self.harness = harness

    def semantic_review(self, payload):
        with self.harness.lock:
            fail = self.harness.semantic_failures > 0
            if fail:
                self.harness.semantic_failures -= 1
        self.harness.journal.append("semantic_review", fault_injected=fail)
        if fail:
            return FakeLLMProvider(review={
                "verdict": "FAIL", "confidence": 1.0,
                "issues": ["M5.2 fault-injected semantic reviewer outage"],
                "summary": "Fault-injected FAIL; committed ledger evidence is retained",
            }).semantic_review(payload)
        return super().semantic_review(payload)


class ArmRequest(BaseModel):
    mode: str
    semantic_failures: int = Field(default=0, ge=0, le=1)


def build_app(settings: Settings, run_id: str, storage_dir: Path,
              journal_dir: Path, *, use_default_container: bool = False):
    """Factory seam for maintained offline tests and the owned Docker entrypoint."""
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise RuntimeError("M5.2 requires an exact invocation UUID")
    if settings.llm_mode != "fake" or not settings.approval_token:
        raise RuntimeError("M5.2 fault backend requires fake mode and approval authorization")
    expected_db = storage_dir / "trustflow.db"
    marker = storage_dir / f".trustflow-owned-{run_id}"
    if Path(settings.db_path or "").absolute() != expected_db.absolute() \
            or storage_dir.is_symlink() or marker.is_symlink() \
            or marker.read_text(encoding="utf-8") != run_id:
        raise RuntimeError("M5.2 SQLite storage ownership mismatch")
    journal = Journal(journal_dir, run_id)
    harness = FaultHarness(journal, settings)
    journal.append("process_started", run_id=run_id)
    original_invoke = ToolRegistry.invoke

    def traced(registry, name, raw_args, idempotency_key=""):
        return harness.invoke(
            lambda tool, args, key: original_invoke(registry, tool, args, key),
            name, raw_args, idempotency_key)

    # Instrument before Container's startup recovery; this records the reads
    # recovery may perform, and would record any unexpected mutation invocation.
    ToolRegistry.invoke = traced
    try:
        module = importlib.import_module("trustflow.api.app")
        box = module.get_container() if use_default_container else module.Container(settings)
    finally:
        ToolRegistry.invoke = original_invoke
    box.registry.invoke = types.MethodType(traced, box.registry)
    box.settings.step_timeout_seconds = settings.step_timeout_seconds
    harness.settings = box.settings
    box.engine.semantic._llm = FaultingFakeProvider(harness)
    journal.append("startup_recovery_complete", recovered_runs=box.recovered_runs)
    app = module.create_app(box)
    app.state.m52_harness = harness

    def authorize(x_approval_token: Annotated[str | None, Header()] = None) -> None:
        if x_approval_token is None:
            raise HTTPException(401, "M5.2 approval authorization required")
        if not hmac.compare_digest(x_approval_token, settings.approval_token or ""):
            raise HTTPException(403, "M5.2 approval authorization rejected")

    @app.post("/__m52/arm", dependencies=[Depends(authorize)])
    def arm(request: ArmRequest):
        harness.arm(request.mode, request.semantic_failures)
        return harness.status()

    @app.post("/__m52/release", dependencies=[Depends(authorize)])
    def release():
        harness.release_event.set()
        harness.journal.append("checkpoint_released")
        return harness.status()

    @app.get("/__m52/status", dependencies=[Depends(authorize)])
    def status():
        return harness.status()

    return app


def create_app():
    """``uvicorn demo.fault_backend:create_app --factory`` (demo image only)."""
    run_id = os.environ.get("TRUSTFLOW_M52_RUN_ID", "")
    settings = Settings(_env_file=None)
    return build_app(settings, run_id, Path("/data"),
                     Path("/tmp") / f"trustflow-m52-{run_id}",
                     use_default_container=True)
