"""Application-inference usage and cost accounting.

Only TrustFlow's own Meta Model API calls are recorded here. Coding-agent
expenses are separate and never mixed in. Token counts are Optional:
unavailable usage is recorded as unknown (None), never fabricated as zero.
Budget enforcement is a conservative application-side estimate (pre-call
reservation against the output cap); it is not a guaranteed provider
billing cap, since the provider may report different final usage.
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
    attempt: int = 0  # logical-call attempt per purpose, assigned by the tracker
    # HTTP attempt within one logical call (provider retries). Logical
    # attempts are counted by the tracker; transport attempts never are.
    transport_attempt: int = 1
    model: str
    workflow_id: str | None = None
    request_id: str | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None  # None = unknown, not zero
    output_tokens: int | None = None
    estimated_cost_usd: float = 0.0
    usage_unknown: bool = False
    over_budget: bool = False
    expense_category: str = "application_inference"
    # Earlier transport attempts of the same logical call, carried on the
    # final record only so callers record exactly one object; the tracker
    # expands them into flat records and clears this field.
    prior_attempts: list["LLMCallRecord"] = []


class UsageTracker:
    """Per-workflow tracker. Records are never dropped, even over budget."""

    def __init__(self, budget_tokens: int, workflow_id: str | None = None) -> None:
        self.budget = budget_tokens
        self.workflow_id = workflow_id
        self.records: list[LLMCallRecord] = []

    @property
    def total_tokens(self) -> int:
        return sum((r.input_tokens or 0) + (r.output_tokens or 0) for r in self.records)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(r.estimated_cost_usd for r in self.records), 8)

    def reserve(self, estimated_tokens: int) -> None:
        """Conservative pre-call check against remaining budget."""
        if self.total_tokens + estimated_tokens > self.budget:
            raise TokenBudgetExceededError(
                f"Token budget {self.budget} cannot reserve {estimated_tokens} "
                f"(already used {self.total_tokens})"
            )

    def record(self, rec: LLMCallRecord) -> LLMCallRecord:
        """Always preserve the incurred record; flag (and signal) over-budget.

        A record carrying ``prior_attempts`` (earlier HTTP attempts of the
        same logical call) is expanded into one flat record per transport
        attempt, all sharing the logical ``attempt`` number, so retries are
        accounted without inflating the logical-call count and no attempt
        is ever double-counted.
        """
        priors = list(rec.prior_attempts)
        rec.prior_attempts = []
        batch = [*priors, rec]
        if rec.attempt == 0:
            logical = 1 + max(
                (r.attempt for r in self.records if r.purpose == rec.purpose),
                default=0,
            )
        else:
            logical = rec.attempt
        for item in batch:
            if item.workflow_id is None:
                item.workflow_id = self.workflow_id
            item.attempt = logical
            item.usage_unknown = (
                item.input_tokens is None or item.output_tokens is None
            )
            item.estimated_cost_usd = estimate_cost(
                item.model, item.input_tokens or 0, item.output_tokens or 0
            )
            self.records.append(item)
        if self.total_tokens > self.budget:
            rec.over_budget = True
            raise TokenBudgetExceededError(
                f"Token budget {self.budget} exceeded ({self.total_tokens} used); "
                "the incurred call remains recorded"
            )
        return rec
