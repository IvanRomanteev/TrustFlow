"""Planner: goal -> typed Plan. Risk is set by PolicyEngine, not the LLM."""

from trustflow.domain.models import Plan, PlanStep, WorkflowGoal
from trustflow.llm.provider import LLMProvider
from trustflow.policies.policy_engine import PolicyEngine


class Planner:
    def __init__(self, llm: LLMProvider, policy: PolicyEngine | None = None) -> None:
        self._llm = llm
        self._policy = policy or PolicyEngine()

    def create_plan(self, goal: WorkflowGoal, version: int = 1) -> Plan:
        raw_steps = self._llm.propose_steps(goal.title, goal.description)
        steps: list[PlanStep] = []
        for idx, raw in enumerate(raw_steps):
            args = {
                k: v
                for k, v in raw.items()
                if k not in ("tool_name", "rationale") and v is not None
            }
            steps.append(
                PlanStep(
                    step_id=f"v{version}-step-{idx + 1}",
                    tool_name=str(raw.get("tool_name", "")),
                    args=args,
                    rationale=str(raw.get("rationale", "")),
                )
            )
        plan = Plan(goal_title=goal.title, version=version, steps=steps)
        return self._policy.apply(plan)
