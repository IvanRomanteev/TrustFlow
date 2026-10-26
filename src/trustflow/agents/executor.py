"""Executor: runs plan steps once. Succeeded steps are never re-run."""

import asyncio
import hashlib
import json

from trustflow.core.settings import Settings
from trustflow.domain.models import Plan, StepResult, StepStatus, WorkflowRun
from trustflow.tools.registry import ToolRegistry


def logical_key(run_id: str, step_tool: str, args: dict) -> str:
    """Stable idempotency key across retries/resumes for the same logical step."""
    canonical = json.dumps({"tool": step_tool, "args": args}, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{run_id}:{step_tool}:{digest}"


class Executor:
    def __init__(self, registry: ToolRegistry, settings: Settings | None = None) -> None:
        self._registry = registry
        self._settings = settings or Settings()

    async def execute(self, run: WorkflowRun, plan: Plan) -> list[StepResult]:
        done = {r.step_id: r for r in run.step_results if r.status == StepStatus.SUCCEEDED}
        results: list[StepResult] = list(run.step_results)
        for step in plan.steps:
            if step.step_id in done:
                continue  # duplicate-execution guard on retry/resume
            key = logical_key(run.run_id, step.tool_name, step.args)
            args = dict(step.args)
            if step.tool_name == "simulate_transfer":
                args["idempotency_key"] = key
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(self._registry.invoke, step.tool_name, args, key),
                    timeout=self._settings.step_timeout_seconds,
                )
                result = StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status=StepStatus.SUCCEEDED if outcome.ok else StepStatus.FAILED,
                    data=outcome.data,
                    error=outcome.error,
                    idempotency_key=key,
                )
            except Exception as exc:  # tool errors become step failures, not crashes
                result = StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status=StepStatus.FAILED,
                    error=str(exc)[:500],
                    idempotency_key=key,
                )
            results = [r for r in results if r.step_id != step.step_id] + [result]
        return results
