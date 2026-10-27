"""Independent semantic review. Advisory: never overrides deterministic FAIL."""

from typing import Any

from pydantic import BaseModel, Field

from trustflow.core.errors import LLMResponseError
from trustflow.domain.models import Plan, SemanticReview, WorkflowGoal
from trustflow.llm.provider import LLMProvider
from trustflow.llm.usage import UsageTracker
from trustflow.tools.ledger import SyntheticLedger


class SemanticReviewInput(BaseModel):
    """Explicit validation contract for model review output."""

    verdict: str = Field(pattern="^(PASS|FAIL)$")
    confidence: float = Field(ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)
    summary: str = ""


class SemanticReviewer:
    """Reviews goal vs approved plan vs independently obtained ledger evidence."""

    def __init__(self, llm: LLMProvider, ledger: SyntheticLedger,
                 max_output_tokens: int = 1200) -> None:
        self._llm = llm
        self._ledger = ledger
        self._reserve_tokens = max_output_tokens

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

    def review(self, goal: WorkflowGoal, plan: Plan,
               tracker: UsageTracker | None = None) -> SemanticReview:
        payload = {
            "goal": {"title": goal.title, "description": goal.description},
            "plan_steps": [
                {"tool_name": s.tool_name, "args": s.args} for s in plan.steps
            ],
            "evidence": self.evidence(plan),
        }
        if tracker is not None:
            import json as _json

            estimated_input = len(_json.dumps(payload, default=str)) // 4 + 128
            tracker.reserve(estimated_input + self._reserve_tokens)
        try:
            raw, record = self._llm.semantic_review(payload)
        except LLMResponseError as exc:
            if tracker is not None and exc.record is not None:
                tracker.record(exc.record)  # type: ignore[arg-type]
            raise
        if tracker is not None:
            tracker.record(record)
        try:
            parsed = SemanticReviewInput.model_validate(raw)
        except Exception as exc:
            raise LLMResponseError(
                f"Malformed semantic review output: {exc}", record=record
            ) from exc
        from trustflow.core.logging import redact

        return SemanticReview(
            verdict=parsed.verdict, confidence=parsed.confidence,
            issues=[redact(str(i)) for i in parsed.issues],
            summary=redact(parsed.summary), model=record.model,
        )
