"""Controller for explicit M5.2 fault-injected recovery experiments.

No arbitrary status construction is used. The backend executes its accepted
state machine, worker timeouts, SQLite commits and startup recovery unchanged.
"""

import time

import httpx

MISSING_EVIDENCE_GOAL = {
    "title": "zzqqxxyy wwkknn",
    "description": "zzqq wwkknn",
    "require_evidence": True,
}
LATE_WORKER_GOAL = {
    "title": "Transfer 25 with a held worker",
    "description": "transfer 25 to bob; explicit test-owned late-worker fault",
}
CRASH_GOAL = {
    "title": "Transfer 5000 at a crash checkpoint",
    "description": "transfer 5000 to bob; explicit test-owned post-commit crash checkpoint",
}


class FaultController:
    """Authenticated loopback controls; journal survives same-container restart."""

    def __init__(self, stack, token: str) -> None:
        self.url = stack.backend_url
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        response = httpx.request(
            method, f"{self.url}/__m52/{path}", json=body,
            headers={"X-Approval-Token": self.token}, timeout=15,
            trust_env=False)
        response.raise_for_status()
        result = response.json()
        assert result.get("fault_injected") is True
        checkpoint_names = set(result["checkpoints"]) | {
            event.get("name") for event in result["events"]
            if event["event"] == "checkpoint"}
        assert "hook_timeout" not in checkpoint_names, "M5.2 checkpoint deadline expired"
        assert "hook_tool_failure" not in checkpoint_names, "Real ledger tool failed"
        return result

    def arm(self, mode: str, *, semantic_failures: int = 0) -> dict:
        return self._request("POST", "arm", {
            "mode": mode, "semantic_failures": semantic_failures})

    def status(self) -> dict:
        return self._request("GET", "status")

    def release(self) -> dict:
        return self._request("POST", "release", {})

    def wait_checkpoint(self, name: str, *, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            result = self.status()
            if name in result["checkpoints"]:
                return result["checkpoints"][name]
            if time.monotonic() >= deadline:
                raise AssertionError(f"M5.2 observable checkpoint not reached: {name}")
            # Polling observable state, never timing a crash by sleep duration.
            time.sleep(0.05)

    def worker_calls(self, *, mutating_only: bool = False) -> list[dict]:
        events = [event for event in self.status()["events"]
                  if event["event"] == "tool_invoked"]
        if mutating_only:
            events = [event for event in events if event["tool"] == "simulate_transfer"]
        return events
