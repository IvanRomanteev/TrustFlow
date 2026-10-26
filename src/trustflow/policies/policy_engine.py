"""Deterministic risk policy. LLM suggestions never decide risk alone."""

from trustflow.core.settings import Settings
from trustflow.domain.models import Plan, PlanStep, RiskLevel

_READ_ONLY = frozenset({"get_balance", "list_transactions", "fx_quote"})
_MUTATING = frozenset({"simulate_transfer"})


class PolicyEngine:
    """Backend-owned risk classification and approval gating."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self.high_threshold = self._settings.high_risk_threshold

    def assess_step(self, step: PlanStep) -> RiskLevel:
        """Return deterministic risk, ignoring step.suggested_risk."""
        if step.tool_name in _READ_ONLY:
            return RiskLevel.LOW
        if step.tool_name in _MUTATING:
            amount = float(step.args.get("amount", 0) or 0)
            if amount > self.high_threshold:
                return RiskLevel.HIGH
            if amount > self.high_threshold / 10:
                return RiskLevel.MEDIUM
            return RiskLevel.LOW
        # Unknown tools are treated as high risk (fail closed).
        return RiskLevel.HIGH

    def apply(self, plan: Plan) -> Plan:
        """Overwrite every step risk with the deterministic assessment."""
        for step in plan.steps:
            step.risk = self.assess_step(step)
        return plan

    def requires_approval(self, plan: Plan) -> bool:
        return any(s.risk == RiskLevel.HIGH for s in plan.steps)

    def check_bounds(self, plan: Plan) -> list[str]:
        issues: list[str] = []
        if len(plan.steps) > self._settings.max_steps:
            issues.append(
                f"Plan has {len(plan.steps)} steps, max is {self._settings.max_steps}"
            )
        return issues
