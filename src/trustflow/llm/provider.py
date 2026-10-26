"""LLMProvider interface, deterministic Fake, and Meta Responses API client.

Tool/plan schemas are flat (arrays of flat objects): no recursive JSON Schema,
which the Meta Model API does not support.
"""

import json
import re
from typing import Any, Protocol

import httpx

from trustflow.core.errors import LLMNotConfiguredError
from trustflow.core.settings import Settings

# Flat plan schema: list of steps, each a flat object. No $ref recursion.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "account_id": {"type": "string"},
                    "from_account": {"type": "string"},
                    "to_account": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["tool_name"],
            },
        }
    },
    "required": ["steps"],
}


class LLMProvider(Protocol):
    def propose_steps(self, goal_title: str, description: str) -> list[dict[str, Any]]: ...


class FakeLLMProvider:
    """Deterministic provider for tests/demos. No network, no keys."""

    def __init__(self, scripted: list[dict[str, Any]] | None = None) -> None:
        self._scripted = scripted

    def propose_steps(self, goal_title: str, description: str) -> list[dict[str, Any]]:
        if self._scripted is not None:
            return [dict(s) for s in self._scripted]
        text = f"{goal_title} {description}"
        amount_match = re.search(r"(\d+(?:\.\d+)?)", text)
        amount = float(amount_match.group(1)) if amount_match else 100.0
        if "transfer" in text.lower() or "send" in text.lower() or "pay" in text.lower():
            return [
                {
                    "tool_name": "get_balance",
                    "account_id": "acc_alice",
                    "rationale": "Check synthetic sender balance first",
                },
                {
                    "tool_name": "simulate_transfer",
                    "from_account": "acc_alice",
                    "to_account": "acc_bob",
                    "amount": amount,
                    "currency": "USD",
                    "rationale": "Simulated transfer (synthetic funds only)",
                },
            ]
        return [
            {
                "tool_name": "get_balance",
                "account_id": "acc_alice",
                "rationale": "Inspect synthetic account",
            }
        ]


class MetaResponsesProvider:
    """Meta Model API via the Responses API. Disabled without an API key."""

    def __init__(self, settings: Settings | None = None, model: str | None = None) -> None:
        self._settings = settings or Settings()
        self.model = model or self._settings.planning_model

    def propose_steps(self, goal_title: str, description: str) -> list[dict[str, Any]]:
        if not self._settings.llm_api_key:
            raise LLMNotConfiguredError("TRUSTFLOW_LLM_API_KEY is not set")
        payload = {
            "model": self.model,
            "input": (
                "Plan synthetic-data tool steps as JSON for this goal. "
                f"Goal: {goal_title}. Details: {description}. "
                "Only use tools: get_balance, list_transactions, simulate_transfer, fx_quote."
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "plan_steps",
                    "schema": PLAN_SCHEMA,
                    "strict": True,
                }
            },
        }
        # Key goes only in the header; it is never logged.
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self._settings.llm_base_url.rstrip('/')}/responses",
                headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
                json=payload,
            )
            resp.raise_for_status()
        data = resp.json()
        raw = data.get("output_text") or "{}"
        parsed = json.loads(raw)
        steps: list[dict[str, Any]] = parsed.get("steps", [])
        return steps
