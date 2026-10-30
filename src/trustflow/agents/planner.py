"""Planner: goal -> validated Plan. At most one repair, then fail closed.

Usage is attributed at call time through the per-workflow tracker passed by
the engine; there is no shared pending-usage queue to leak between runs.
"""

from typing import Any

from pydantic import BaseModel

from trustflow.core.errors import LLMResponseError, PlanValidationError
from trustflow.domain.models import Plan, PlanStep, WorkflowGoal
from trustflow.llm.provider import LLMProvider
from trustflow.llm.usage import UsageTracker
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.ledger import (
    FxQuoteArgs,
    GetBalanceArgs,
    ListTransactionsArgs,
    SimulateTransferArgs,
)

ARG_MODELS: dict[str, type[BaseModel]] = {
    "get_balance": GetBalanceArgs,
    "list_transactions": ListTransactionsArgs,
    "simulate_transfer": SimulateTransferArgs,
    "fx_quote": FxQuoteArgs,
}
KNOWN_ACCOUNTS = frozenset({"acc_alice", "acc_bob", "acc_ops"})


def validate_steps(raw_steps: Any) -> list[PlanStep]:
    """Allowlist + Pydantic schema + deterministic preconditions. Normalized args."""
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanValidationError("Plan must contain at least one step")
    steps: list[PlanStep] = []
    for idx, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PlanValidationError(f"Step {idx + 1} is not an object")
        tool = str(raw.get("tool_name", ""))
        if tool not in ARG_MODELS:
            raise PlanValidationError(f"Unknown tool in plan: {tool or '<empty>'}")
        # operation_id is backend-owned; any model-supplied value is discarded.
        raw_args = {k: v for k, v in raw.items()
                    if k not in ("tool_name", "rationale", "operation_id")
                    and v is not None and v != ""}
        try:
            parsed = ARG_MODELS[tool].model_validate(raw_args)
        except Exception as exc:
            raise PlanValidationError(f"Invalid args for {tool}: {exc}") from exc
        args = parsed.model_dump(exclude_none=True)
        args.pop("idempotency_key", None)  # engine-owned, never model-supplied
        if tool == "simulate_transfer":
            if args.get("from_account") == args.get("to_account"):
                raise PlanValidationError("Transfer source and destination must differ")
            for acct in (args.get("from_account"), args.get("to_account")):
                if acct not in KNOWN_ACCOUNTS:
                    raise PlanValidationError(f"Unknown synthetic account: {acct}")
        if tool in ("get_balance", "list_transactions"):
            if args.get("account_id") not in KNOWN_ACCOUNTS:
                raise PlanValidationError(f"Unknown synthetic account: {args.get('account_id')}")
        steps.append(PlanStep(step_id=f"step-{idx + 1}", tool_name=tool, args=args,
                              rationale=str(raw.get("rationale", ""))))
    return steps


class Planner:
    def __init__(self, llm: LLMProvider, policy: PolicyEngine | None = None,
                 max_output_tokens: int = 1200) -> None:
        self._llm = llm
        self._policy = policy or PolicyEngine()
        self._reserve_tokens = max_output_tokens

    # The provider truncates retrieved context to this bound; the
    # planner bounds once here so reservation and call use the
    # identical transmitted text (R6).
    _CONTEXT_CHARS = 2000

    def _accepts_retrieval_context(self) -> bool:
        """Capability is agreed with the actual callable signature,
        never inherited blindly (R8): a legacy override that does not
        accept ``retrieval_context`` (and has no ``**kwargs``) is never
        passed the keyword. No TypeError-based retry is used."""
        import inspect

        try:
            params = inspect.signature(self._llm.propose_plan).parameters
        except (TypeError, ValueError):
            return False
        return "retrieval_context" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _propose(self, goal: WorkflowGoal, tracker: UsageTracker | None,
                 feedback: str | None = None,
                 retrieval_context: str | None = None,
                 retrieval_mandatory: bool = False,
                 retrieval_shrinkable: bool = False):
        # Capability and the actual transmitted arguments are decided
        # BEFORE any reservation (R6/N2): a legacy callable that does
        # not accept the context never has it reserved.
        accepts = bool(retrieval_context) and self._accepts_retrieval_context()
        transmitted = (
            (retrieval_context or "")[: self._CONTEXT_CHARS] if accepts else "")
        base_len = len(goal.title) + len(goal.description)

        def estimate(context_len: int) -> int:
            return (base_len + context_len) // 4 + 256 + self._reserve_tokens

        estimated = estimate(len(transmitted))
        if tracker is not None:
            if transmitted and retrieval_shrinkable \
                    and not retrieval_mandatory \
                    and estimated > tracker.budget - tracker.total_tokens:
                # Optional context is shrunk to what fits the remaining
                # budget before reservation (R5/F3), identically for
                # initial, repair and transport-retry calls. If even
                # the context-free estimate does not fit, the honest
                # reservation failure below is preserved.
                room = tracker.budget - tracker.total_tokens - 256 \
                    - self._reserve_tokens
                max_len = max(0, (room + 1) * 4 - 1 - base_len) \
                    if room >= 0 else 0
                transmitted = transmitted[:max_len]
                estimated = estimate(len(transmitted))
            # Mandatory context is never silently shrunk: its full
            # reservation either fits or fails before execution.
            tracker.reserve(estimated)
        context_kw: dict[str, Any] = {}
        if transmitted:
            context_kw["retrieval_context"] = transmitted
            # The transport layer may shrink optional context at retry
            # only when the caller marked it shrinkable (F3); it is
            # told explicitly, never inferred from the context (R6).
            import inspect

            try:
                params: dict[str, Any] = dict(
                    inspect.signature(self._llm.propose_plan).parameters)
            except (TypeError, ValueError):
                params = {}
            if "context_shrinkable" in params:
                context_kw["context_shrinkable"] = retrieval_shrinkable
        try:
            propose = self._llm.propose_plan
            if tracker is not None and getattr(
                    self._llm, "supports_attempt_tracker", False):
                # Workflow-bound mode: the provider records each transport
                # attempt in this tracker and re-reserves before retrying.
                raw, record = propose(  # type: ignore[call-arg]
                    goal.title, goal.description, repair_feedback=feedback,
                    tracker=tracker, retry_reserve_tokens=estimated,
                    **context_kw)
            else:
                raw, record = propose(goal.title, goal.description,
                                      repair_feedback=feedback, **context_kw)
        except LLMResponseError as exc:
            if tracker is not None and exc.record is not None:
                tracker.record(exc.record)  # type: ignore[arg-type]
            raise
        if tracker is not None:
            tracker.record(record)
        return raw

    def create_plan(self, goal: WorkflowGoal, version: int = 1,
                    tracker: UsageTracker | None = None,
                    retrieval_context: str | None = None,
                    retrieval_mandatory: bool = False,
                    retrieval_shrinkable: bool = False) -> Plan:
        raw = self._propose(goal, tracker, retrieval_context=retrieval_context,
                            retrieval_mandatory=retrieval_mandatory,
                            retrieval_shrinkable=retrieval_shrinkable)
        try:
            steps = validate_steps(raw)
        except PlanValidationError as first_error:
            raw = self._propose(goal, tracker, feedback=str(first_error),
                                retrieval_context=retrieval_context,
                                retrieval_mandatory=retrieval_mandatory,
                                retrieval_shrinkable=retrieval_shrinkable)
            steps = validate_steps(raw)  # second failure propagates: fail closed
        for idx, step in enumerate(steps):
            step.step_id = f"v{version}-step-{idx + 1}"
        plan = Plan(goal_title=goal.title, version=version, steps=steps)
        return self._policy.apply(plan)
