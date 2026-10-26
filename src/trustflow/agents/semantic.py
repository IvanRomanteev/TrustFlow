"""Independent semantic review. Advisory: never overrides deterministic FAIL."""

from typing import Any

from trustflow.domain.models import Plan, SemanticReview, WorkflowGoal
from trustflow.llm.provider import LLMProvider
from trustflow.llm.usage import LLMCallRecord
from trustflow.tools.ledger import SyntheticLedger


class SemanticReviewer:
    """Reviews goal vs approved plan vs independently obtained ledger evidence."""

    def __init__(self, llm: LLMProvider, ledger: SyntheticLedger) -> None:
        self._llm = llm
        self._ledger = ledger
        self.calls: list[LLMCallRecord] = []

    def evidence(self, plan: Plan) -> dict[str, Any]:
        """Evidence read directly from the ledger, not from executor output."""
        accounts: set[str] = set()
        for step in plan.steps:
            for key in ("account_id", "from_account", "to_account"):
                if step.args.get(key):
                    accounts.add(str(step.args[key]))
        balances = {}
        transactions: list[dict] = []
        for acct in sorted(accounts):
            try:
                balances[acct] = self._ledger.get_balance(acct).model_dump()
                transactions.extend(
                    t.model_dump() for t in self._ledger.list_transactions(acct, 50)
                )
            except KeyError:
                continue
        unique = {t["tx_id"]: t for t in transactions}
        return {"balances": balances, "transactions": list(unique.values())}

    def review(self, goal: WorkflowGoal, plan: Plan) -> SemanticReview:
        payload = {
            "goal": {"title": goal.title, "description": goal.description},
            "plan_steps": [
                {"tool_name": s.tool_name, "args": s.args} for s in plan.steps
            ],
            "evidence": self.evidence(plan),
        }
        raw, record = self._llm.semantic_review(payload)
        self.calls.append(record)
        return SemanticReview(
            verdict=str(raw.get("verdict", "FAIL")),
            confidence=float(raw.get("confidence", 0) or 0),
            issues=[str(i) for i in raw.get("issues", [])],
            summary=str(raw.get("summary", "")),
            model=record.model,
        )
