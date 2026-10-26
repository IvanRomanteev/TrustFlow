"""Executor: runs steps once. Committed transfers are never repeated."""

import asyncio
import hashlib
import json

from trustflow.core.settings import Settings
from trustflow.domain.models import Plan, StepResult, StepStatus, WorkflowRun
from trustflow.tools.registry import ToolRegistry


def logical_key(run_id: str, step_tool: str, args: dict) -> str:
    canonical = json.dumps({"tool": step_tool, "args": args}, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{run_id}:{step_tool}:{digest}"


def operation_key(step_operation_id: str, fallback: str) -> str:
    return step_operation_id or fallback


class Executor:
    def __init__(self, registry: ToolRegistry, settings: Settings | None = None) -> None:
        self._registry = registry
        self._settings = settings or Settings()

    async def execute(self, run: WorkflowRun, plan: Plan) -> list[StepResult]:
        done = {r.step_id: r for r in run.step_results if r.status == StepStatus.SUCCEEDED}
        # A transfer committed under any earlier plan version also blocks replays
        # whose step_id changed after re-planning (matched by operation key).
        done_ops = {
            r.idempotency_key for r in run.step_results if r.status == StepStatus.SUCCEEDED
        }
        results: list[StepResult] = list(run.step_results)
        for step in plan.steps:
            key = operation_key(
                step.operation_id, logical_key(run.run_id, step.tool_name, step.args)
            )
            if step.step_id in done or key in done_ops:
                continue
            args = dict(step.args)
            if step.tool_name == "simulate_transfer":
                args["idempotency_key"] = key
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(self._registry.invoke, step.tool_name, args, key),
                    timeout=self._settings.step_timeout_seconds,
                )
                result = StepResult(
                    step_id=step.step_id, tool_name=step.tool_name,
                    status=StepStatus.SUCCEEDED if outcome.ok else StepStatus.FAILED,
                    data=outcome.data, error=outcome.error, idempotency_key=key,
                )
            except Exception as exc:
                # Timeout after a successful mutation: the worker thread may have
                # committed. Reconcile against the ledger before failing the step.
                committed = (
                    self._registry.committed(key)
                    if step.tool_name == "simulate_transfer" else None
                )
                if committed is not None:
                    result = StepResult(
                        step_id=step.step_id, tool_name=step.tool_name,
                        status=StepStatus.SUCCEEDED,
                        data={**committed.model_dump(), "reconciled_after_timeout": True},
                        idempotency_key=key,
                    )
                else:
                    result = StepResult(
                        step_id=step.step_id, tool_name=step.tool_name,
                        status=StepStatus.FAILED, error=str(exc)[:500],
                        idempotency_key=key,
                    )
            results = [r for r in results if r.step_id != step.step_id] + [result]
        return results
