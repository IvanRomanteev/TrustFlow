"""LLMProvider: Fake (offline) and Meta Responses API (live). No silent fallback.

Verified against official docs (dev.meta.ai/docs/structured-output,
/docs/pricing-rate-limits, /docs/quickstart): POST {base}/responses with
text.format={type: json_schema, name, schema, strict}; recursive schemas are
rejected (HTTP 400), so all TrustFlow schemas are flat. Live errors raise;
the caller never substitutes Fake output for a failed live call.
"""

import json
import re
import time
from typing import Any, Protocol

import httpx

from trustflow.core.errors import LLMNotConfiguredError, LLMResponseError
from trustflow.core.settings import Settings
from trustflow.llm.usage import LLMCallRecord

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool_name": {"type": "string"},
                    "account_id": {"type": "string"},
                    "from_account": {"type": "string"},
                    "to_account": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                # All fields required (strict-subset style): the model emits
                # "" or 0 for fields a given tool does not use; Pydantic arg
                # models drop the extras per tool.
                "required": ["tool_name", "account_id", "from_account",
                             "to_account", "amount", "currency", "rationale"],
            },
        }
    },
    "required": ["steps"],
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "confidence": {"type": "number"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "issues"],
}

TOOLS_HINT = "Only use tools: get_balance, list_transactions, simulate_transfer, fx_quote."


class LLMProvider(Protocol):
    def propose_plan(
        self, goal_title: str, description: str, repair_feedback: str | None = None
    ) -> tuple[list[dict[str, Any]], LLMCallRecord]: ...

    def semantic_review(self, payload: dict[str, Any]) -> tuple[dict[str, Any], LLMCallRecord]: ...


class FakeLLMProvider:
    """Deterministic offline provider. Zero cost, no network, no keys."""

    model = "fake"

    def __init__(self, scripted: list[dict[str, Any]] | None = None,
                 review: dict[str, Any] | None = None) -> None:
        self._scripted = scripted
        self._review = review
        self.repair_calls = 0

    def propose_plan(
        self, goal_title: str, description: str, repair_feedback: str | None = None
    ) -> tuple[list[dict[str, Any]], LLMCallRecord]:
        if repair_feedback is not None:
            self.repair_calls += 1
        if self._scripted is not None:
            steps = [dict(s) for s in self._scripted]
        else:
            text = f"{goal_title} {description}"
            match = re.search(r"(\d+(?:\.\d+)?)", text)
            amount = float(match.group(1)) if match else 100.0
            if any(w in text.lower() for w in ("transfer", "send", "pay")):
                steps = [
                    {"tool_name": "get_balance", "account_id": "acc_alice",
                     "rationale": "Check synthetic sender balance first"},
                    {"tool_name": "simulate_transfer", "from_account": "acc_alice",
                     "to_account": "acc_bob", "amount": amount, "currency": "USD",
                     "rationale": "Simulated transfer (synthetic funds only)"},
                ]
            else:
                steps = [{"tool_name": "get_balance", "account_id": "acc_alice",
                          "rationale": "Inspect synthetic account"}]
        purpose = "repair" if repair_feedback else "plan"
        return steps, LLMCallRecord(purpose=purpose, model=self.model)

    # Backwards-compatible M0 helper.
    def propose_steps(self, goal_title: str, description: str) -> list[dict[str, Any]]:
        steps, _ = self.propose_plan(goal_title, description)
        return steps

    def semantic_review(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], LLMCallRecord]:
        if self._review is not None:
            return dict(self._review), LLMCallRecord(purpose="semantic_review", model=self.model)
        has_transfer = any(
            s.get("tool_name") == "simulate_transfer" for s in payload.get("plan_steps", [])
        )
        txs = payload.get("evidence", {}).get("transactions", [])
        ok = bool(txs) if has_transfer else True
        review = {"verdict": "PASS" if ok else "FAIL", "confidence": 1.0,
                  "issues": [] if ok else ["No ledger transaction evidence for transfer"],
                  "summary": "fake deterministic review"}
        return review, LLMCallRecord(purpose="semantic_review", model=self.model)


def _extract_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return str(data["output_text"])
    for item in data.get("output", []) or []:
        for part in item.get("content", []) or []:
            if isinstance(part.get("text"), str) and part["text"]:
                return str(part["text"])
    raise LLMResponseError("Live response contained no output text")


class MetaResponsesProvider:
    """Meta Model API via Responses API. Raises on any failure (fail closed)."""

    def __init__(self, settings: Settings | None = None, model: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings or Settings()
        self.model = model or self._settings.planning_model
        self._transport = transport  # test seam only; production uses network

    def _call(self, name: str, schema: dict, prompt: str, purpose: str,
              model: str | None = None) -> tuple[dict[str, Any], LLMCallRecord]:
        key = self._settings.model_api_key
        if not key:
            raise LLMNotConfiguredError("MODEL_API_KEY is not set (live mode)")
        payload = {
            "model": model or self.model,
            "input": prompt,
            "max_output_tokens": self._settings.max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": name,
                                "schema": schema, "strict": False}},
        }
        url = f"{self._settings.llm_base_url.rstrip('/')}/responses"
        last_error: Exception | None = None
        attempts = 1 + self._settings.llm_max_retries
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                with httpx.Client(timeout=self._settings.llm_timeout_seconds,
                                  transport=self._transport) as client:
                    resp = client.post(url, headers={"Authorization": f"Bearer {key}"},
                                       json=payload)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    time.sleep(self._settings.llm_backoff_seconds * (2 ** attempt))
                    continue
                resp.raise_for_status()
                data = resp.json()
                parsed = json.loads(_extract_text(data))
                if not isinstance(parsed, dict):
                    raise LLMResponseError("Live response JSON was not an object")
                usage = data.get("usage") or {}
                record = LLMCallRecord(
                    purpose=purpose, model=str(payload["model"]),
                    request_id=data.get("id") or resp.headers.get("x-request-id"),
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                )
                return parsed, record
            except (httpx.HTTPError, json.JSONDecodeError, LLMResponseError) as exc:
                last_error = exc
                if attempt < attempts - 1 and isinstance(exc, httpx.TransportError):
                    time.sleep(self._settings.llm_backoff_seconds * (2 ** attempt))
                    continue
                raise LLMResponseError(f"Live model call failed: {exc}") from exc
        raise LLMResponseError(f"Live model call failed: {last_error}")

    def propose_plan(self, goal_title, description, repair_feedback=None):
        prompt = (
            "Plan synthetic-data tool steps as JSON for this goal. "
            f"Goal: {goal_title}. Details: {description}. {TOOLS_HINT} "
            "Accounts: acc_alice, acc_bob, acc_ops. Currency USD. "
            "Every step must set every field: use account_id for "
            "get_balance/list_transactions; from_account, to_account, amount "
            "and currency for simulate_transfer; empty string or 0 elsewhere. "
            'Example: {"tool_name": "simulate_transfer", "account_id": "", '
            '"from_account": "acc_alice", "to_account": "acc_bob", '
            '"amount": 100, "currency": "USD", "rationale": "pay"}'
        )
        if repair_feedback:
            prompt += f" Previous plan was rejected: {repair_feedback}. Fix it."
        parsed, record = self._call("plan_steps", PLAN_SCHEMA, prompt,
                                    "repair" if repair_feedback else "plan")
        steps = parsed.get("steps")
        if not isinstance(steps, list):
            raise LLMResponseError("Live plan response missing steps list")
        return steps, record

    def semantic_review(self, payload):
        prompt = (
            "Independently review whether the executed synthetic workflow satisfies "
            "the user goal. Judge only from the supplied goal, approved plan and "
            "independently obtained ledger evidence. Return the review JSON.\n"
            + json.dumps(payload, default=str)[:6000]
        )
        return self._call("semantic_review", REVIEW_SCHEMA, prompt,
                          "semantic_review", model=self._settings.review_model)


def build_provider(settings: Settings) -> LLMProvider:
    """Single explicit switch. live -> Meta (raises without key), else Fake."""
    if settings.llm_mode == "live":
        if not settings.model_api_key:
            raise LLMNotConfiguredError("TRUSTFLOW_LLM_MODE=live requires MODEL_API_KEY")
        return MetaResponsesProvider(settings)
    return FakeLLMProvider()
