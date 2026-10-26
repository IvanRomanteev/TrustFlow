"""Application-inference usage and cost accounting.

Only TrustFlow's own Meta Model API calls are recorded here. Coding-agent
(Muse) expenses are a separate subscription and are never mixed in.
Official standard pricing (per 1M tokens, dev.meta.ai/docs/pricing-rate-limits):
wonder $4 in / $20 out, spark $1.25 in / $4.25 out. Cached input ignored (M1).
"""

from pydantic import BaseModel

from trustflow.core.errors import TokenBudgetExceededError

PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    "muse-wonder-1.0": (4.0, 20.0),
    "muse-spark-1.3": (1.25, 4.25),
    "fake": (0.0, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = PRICING_PER_MILLION.get(model, (4.0, 20.0))
    return round((input_tokens * rate_in + output_tokens * rate_out) / 1_000_000, 8)


class LLMCallRecord(BaseModel):
    purpose: str  # plan | repair | semantic_review
    model: str
    request_id: str | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    expense_category: str = "application_inference"


class UsageTracker:
    """Per-workflow token budget. Exceeding it fails closed."""

    def __init__(self, budget_tokens: int) -> None:
        self.budget = budget_tokens
        self.records: list[LLMCallRecord] = []

    @property
    def total_tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.records)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(r.estimated_cost_usd for r in self.records), 8)

    def record(self, rec: LLMCallRecord) -> LLMCallRecord:
        rec.estimated_cost_usd = estimate_cost(rec.model, rec.input_tokens, rec.output_tokens)
        if self.total_tokens + rec.input_tokens + rec.output_tokens > self.budget:
            raise TokenBudgetExceededError(
                f"Token budget {self.budget} would be exceeded "
                f"({self.total_tokens}+{rec.input_tokens + rec.output_tokens})"
            )
        self.records.append(rec)
        return rec
