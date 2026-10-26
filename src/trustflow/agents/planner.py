"""Planner: goal -> validated Plan. At most one repair, then fail closed."""

from typing import Any

from pydantic import BaseModel

from trustflow.core.errors import PlanValidationError
from trustflow.domain.models import Plan, PlanStep, WorkflowGoal
from trustflow.llm.provider import LLMProvider
from trustflow.llm.usage import LLMCallRecord
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
        raw_args = {k: v for k, v in raw.items()
                    if k not in ("tool_name", "rationale") and v is not None}
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
    def __init__(self, llm: LLMProvider, policy: PolicyEngine | None = None) -> None:
        self._llm = llm
        self._policy = policy or PolicyEngine()
        self.calls: list[LLMCallRecord] = []

    def create_plan(self, goal: WorkflowGoal, version: int = 1) -> Plan:
        raw, record = self._llm.propose_plan(goal.title, goal.description)
        self.calls.append(record)
        try:
            steps = validate_steps(raw)
        except PlanValidationError as first_error:
            # Exactly one schema-repair attempt, then fail closed.
            raw, record = self._llm.propose_plan(
                goal.title, goal.description, repair_feedback=str(first_error)
            )
            self.calls.append(record)
            steps = validate_steps(raw)
        for idx, step in enumerate(steps):
            step.step_id = f"v{version}-step-{idx + 1}"
        plan = Plan(goal_title=goal.title, version=version, steps=steps)
        return self._policy.apply(plan)
