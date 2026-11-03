"""Executor: reads always refresh; mutations reconcile against the ledger.

A timeout never declares a definitive mutation failure while its worker can
still commit: the step becomes UNCERTAIN, the worker task is tracked, and no
conflicting replacement runs until reconciliation resolves the outcome.
"""

import asyncio
import copy
import hashlib
import json

from trustflow.core.logging import sanitize_error
from trustflow.core.settings import Settings
from trustflow.domain.models import Plan, PlanStep, StepResult, StepStatus, WorkflowRun
from trustflow.tools.registry import ToolRegistry


def logical_key(run_id: str, step_tool: str, args: dict) -> str:
    canonical = json.dumps({"tool": step_tool, "args": args}, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{run_id}:{step_tool}:{digest}"


def payload_matches(step: PlanStep, tx) -> bool:
    return (
        tx.from_account == step.args.get("from_account")
        and tx.to_account == step.args.get("to_account")
        and float(tx.amount) == float(step.args.get("amount", -1))
        and tx.currency == step.args.get("currency", "USD")
    )


class Executor:
    def __init__(self, registry: ToolRegistry, settings: Settings | None = None) -> None:
        self._registry = registry
        self._settings = settings or Settings()
        self._pending: dict[str, asyncio.Task] = {}

    def _key(self, run: WorkflowRun, step: PlanStep) -> str:
        return step.operation_id or logical_key(run.run_id, step.tool_name, step.args)

    async def _invoke(self, step: PlanStep, args: dict, key: str) -> StepResult:
        task = asyncio.ensure_future(
            asyncio.to_thread(self._registry.invoke, step.tool_name, args, key)
        )
        # Every worker is tracked: cancelling an await never stops a thread.
        self._pending[key] = task
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(task), timeout=self._settings.step_timeout_seconds
            )
            return StepResult(
                step_id=step.step_id, tool_name=step.tool_name,
                status=StepStatus.SUCCEEDED if outcome.ok else StepStatus.FAILED,
                data=outcome.data, error=outcome.error, idempotency_key=key,
            )
        except TimeoutError:
            # Worker may still finish/commit. Track it; do not fail definitively.
            return StepResult(
                step_id=step.step_id, tool_name=step.tool_name,
                status=StepStatus.UNCERTAIN,
                error="Timed out; outcome uncertain, worker still tracked",
                idempotency_key=key,
            )
        except Exception as exc:
            committed = (
                self._registry.committed(key)
                if step.tool_name == "simulate_transfer" else None
            )
            if committed is not None and payload_matches(step, committed):
                return StepResult(
                    step_id=step.step_id, tool_name=step.tool_name,
                    status=StepStatus.SUCCEEDED,
                    data={**committed.model_dump(), "reconciled_from_ledger": True},
                    idempotency_key=key,
                )
            return StepResult(
                step_id=step.step_id, tool_name=step.tool_name,
                status=StepStatus.FAILED, error=sanitize_error(str(exc)),
                idempotency_key=key,
            )
        finally:
            if task.done():
                self._pending.pop(key, None)

    async def execute(self, run: WorkflowRun, plan: Plan) -> list[StepResult]:
        results: list[StepResult] = []
        for step in plan.steps:
            args = copy.deepcopy(step.args)  # approved snapshot stays untouched
            key = self._key(run, step)
            if step.tool_name == "simulate_transfer":
                # Mutation boundary: only an authorized manifest intent whose
                # payload fingerprint still matches, inside the exact approved
                # snapshot (when one exists), may ever reach the ledger.
                entry = next(
                    (i for i in run.intents
                     if i.intent_id == (step.intent_id or step.operation_id)),
                    None,
                )
                from trustflow.orchestration.engine import fingerprint_of

                boundary_ok = bool(
                    entry is not None
                    and entry.authorized
                    and entry.fingerprint
                    == fingerprint_of(step.tool_name, entry.authorized_payload)
                    and fingerprint_of(step.tool_name, step.args)
                    == fingerprint_of(step.tool_name, entry.authorized_payload)
                    and (
                        not run.approved_hash
                        or plan.plan_hash == run.approved_hash
                    )
                )
                if not boundary_ok:
                    results.append(StepResult(
                        step_id=step.step_id, tool_name=step.tool_name,
                        status=StepStatus.FAILED,
                        error="Operation not authorized at mutation boundary",
                        idempotency_key=key,
                    ))
                    continue
                committed = self._registry.committed(key)
                if committed is not None:
                    if payload_matches(step, committed):
                        results.append(StepResult(
                            step_id=step.step_id, tool_name=step.tool_name,
                            status=StepStatus.SUCCEEDED,
                            data={**committed.model_dump(),
                                  "reconciled_from_ledger": True},
                            idempotency_key=key,
                        ))
                    else:
                        results.append(StepResult(
                            step_id=step.step_id, tool_name=step.tool_name,
                            status=StepStatus.FAILED,
                            error="Committed operation conflicts with plan payload",
                            idempotency_key=key,
                        ))
                    continue
                if key in self._pending and not self._pending[key].done():
                    results.append(StepResult(
                        step_id=step.step_id, tool_name=step.tool_name,
                        status=StepStatus.UNCERTAIN,
                        error="Worker still in flight; replacement blocked",
                        idempotency_key=key,
                    ))
                    continue
                args["idempotency_key"] = key
            results.append(await self._invoke(step, args, key))
        return results

    async def reconcile(self, run: WorkflowRun, plan: Plan) -> list[StepResult]:
        """Resolve UNCERTAIN steps from tracked workers / the ledger only."""
        await asyncio.sleep(0)  # let worker-completion callbacks run
        updated: list[StepResult] = []
        by_id = {r.step_id: r for r in run.step_results}
        for step in plan.steps:
            current = by_id.get(step.step_id)
            if current is None or current.status != StepStatus.UNCERTAIN:
                if current is not None:
                    if (
                        step.tool_name != "simulate_transfer"
                        and current.status == StepStatus.FAILED
                    ):
                        # Reads carry no mutation risk: refresh them fresh
                        # instead of reusing a stale timed-out result.
                        updated.append(
                            await self._invoke(step, copy.deepcopy(step.args),
                                               self._key(run, step))
                        )
                    else:
                        updated.append(current)
                continue
            key = self._key(run, step)
            committed = self._registry.committed(key)
            task = self._pending.get(key)
            if committed is not None and payload_matches(step, committed):
                updated.append(StepResult(
                    step_id=step.step_id, tool_name=step.tool_name,
                    status=StepStatus.SUCCEEDED,
                    data={**committed.model_dump(), "reconciled_from_ledger": True},
                    idempotency_key=key,
                ))
                if task is not None and task.done():
                    self._pending.pop(key, None)
            elif task is None:
                # No live worker tracks this step (e.g. after a process
                # restart workers cannot survive) and the ledger has no
                # commit: the operation definitively did not commit.
                updated.append(StepResult(
                    step_id=step.step_id, tool_name=step.tool_name,
                    status=StepStatus.FAILED,
                    error="Worker no longer tracked and no ledger commit; "
                          "operation was not committed",
                    idempotency_key=key,
                ))
            elif task is not None and task.done():
                self._pending.pop(key, None)
                try:
                    outcome = task.result()
                    updated.append(StepResult(
                        step_id=step.step_id, tool_name=step.tool_name,
                        status=StepStatus.SUCCEEDED if outcome.ok else StepStatus.FAILED,
                        data=outcome.data, error=outcome.error, idempotency_key=key,
                    ))
                except Exception as exc:
                    updated.append(StepResult(
                        step_id=step.step_id, tool_name=step.tool_name,
                        status=StepStatus.FAILED, error=sanitize_error(str(exc)),
                        idempotency_key=key,
                    ))
            else:
                updated.append(current)  # still unresolved
        return updated
