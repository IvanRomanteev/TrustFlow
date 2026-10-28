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
from trustflow.llm.usage import MAX_TOKEN_COUNT, LLMCallRecord, UsageTracker

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
                    "from_currency": {"type": "string"},
                    "to_currency": {"type": "string"},
                    # Same bounds as the local ListTransactionsArgs
                    # contract (ge=1, le=100); see docs/m1.md for the
                    # flat-schema limits that cannot be expressed per tool.
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "rationale": {"type": "string"},
                },
                # All fields required (strict-subset style): the model emits
                # "" or 0 for fields a given tool does not use; Pydantic arg
                # models drop the extras per tool.
                "required": ["tool_name", "account_id", "from_account",
                             "to_account", "amount", "currency",
                             "from_currency", "to_currency", "limit", "rationale"],
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
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "confidence", "issues", "summary"],
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
    """Validate the envelope structure before reading any text from it.

    Anything unexpected in external data raises LLMResponseError; this
    function never indexes into unvalidated values.
    """
    if not isinstance(data, dict):
        raise LLMResponseError("Live response envelope was not an object")
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return str(data["output_text"])
    output = data.get("output")
    if output is not None and not isinstance(output, list):
        raise LLMResponseError("Live response output was not a list")
    for item in output or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                return str(part["text"])
    raise LLMResponseError("Live response contained no output text")


def _token_count(value: object) -> int | None:
    """A valid provider token count, else None (unknown, never invented).

    Counts above MAX_TOKEN_COUNT cannot be accounted reliably and are
    recorded as unknown; the other token field of the same response is
    unaffected, so known usage is never lost with the unusable part.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= MAX_TOKEN_COUNT:
        return value
    if isinstance(value, float) and 0 <= value <= MAX_TOKEN_COUNT and value.is_integer():
        return int(value)
    return None


def _attempt_error(message: str, records: list[LLMCallRecord]) -> LLMResponseError:
    """Error carrying every transport attempt; the tracker expands the
    prior attempts attached to the final record, so nothing is lost and
    nothing is counted twice."""
    if records:
        records[-1].prior_attempts = list(records[:-1])
        return LLMResponseError(message, records=records)
    return LLMResponseError(message)


class MetaResponsesProvider:
    """Meta Model API via Responses API. Raises on any failure (fail closed)."""

    # Callers with a per-workflow UsageTracker pass it per call (never
    # shared provider state), so transport retries are accounted and
    # re-reserved in the originating workflow before the next request.
    supports_attempt_tracker = True

    def __init__(self, settings: Settings | None = None, model: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings or Settings()
        self.model = model or self._settings.planning_model
        self._transport = transport  # test seam only; production uses network

    def _call(self, name: str, schema: dict, prompt: str, purpose: str,
              model: str | None = None,
              tracker: UsageTracker | None = None,
              retry_reserve_tokens: int = 0) -> tuple[dict[str, Any], LLMCallRecord]:
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
        attempts = 1 + self._settings.llm_max_retries
        # One record per HTTP attempt, built before any retry/raise so
        # retryable errors, timeouts and terminal failures are all
        # accounted. Without a tracker, only the final record is
        # returned/raised, carrying the earlier ones as prior_attempts
        # (no double counting). With a tracker (workflow-bound mode),
        # every interim/failed attempt is recorded into the originating
        # workflow's tracker immediately, the next attempt must pass a
        # fresh reservation against the now-known usage, and the final
        # successful record is returned for the caller to record once.
        # Unknown usage consumes no known tokens, so the re-reservation
        # re-checks the full per-call estimate against known totals; the
        # provider's actual billing for unknown attempts is not bounded
        # by this application-side check.
        attempt_records: list[LLMCallRecord] = []
        logical_attempt = 0

        def account_before_retry(record: LLMCallRecord) -> None:
            """Record an interim attempt in the originating workflow and
            require a fresh reservation before any next HTTP request."""
            nonlocal logical_attempt
            assert tracker is not None
            if logical_attempt:
                record.attempt = logical_attempt
            tracker.record(record)  # incurred usage survives budget errors
            logical_attempt = record.attempt
            tracker.reserve(retry_reserve_tokens)

        for attempt in range(attempts):
            transport_attempt = attempt + 1
            started = time.perf_counter()
            try:
                with httpx.Client(timeout=self._settings.llm_timeout_seconds,
                                  transport=self._transport) as client:
                    resp = client.post(url, headers={"Authorization": f"Bearer {key}"},
                                       json=payload)
            except httpx.TransportError as exc:
                unknown = LLMCallRecord(
                    purpose=purpose, model=str(payload["model"]),
                    transport_attempt=transport_attempt,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                if tracker is not None:
                    if attempt < attempts - 1:
                        account_before_retry(unknown)
                        time.sleep(self._settings.llm_backoff_seconds * (2 ** attempt))
                        continue
                    if logical_attempt:
                        unknown.attempt = logical_attempt
                    tracker.record(unknown)
                    raise LLMResponseError(
                        f"Live model call failed: {exc}") from exc
                attempt_records.append(unknown)
                if attempt < attempts - 1:
                    time.sleep(self._settings.llm_backoff_seconds * (2 ** attempt))
                    continue
                raise _attempt_error(
                    f"Live model call failed: {exc}", attempt_records
                ) from exc
            except httpx.HTTPError as exc:
                unknown = LLMCallRecord(
                    purpose=purpose, model=str(payload["model"]),
                    transport_attempt=transport_attempt,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                if tracker is not None:
                    if logical_attempt:
                        unknown.attempt = logical_attempt
                    tracker.record(unknown)
                    raise LLMResponseError(
                        f"Live model call failed: {exc}") from exc
                attempt_records.append(unknown)
                raise _attempt_error(
                    f"Live model call failed: {exc}", attempt_records
                ) from exc
            # Usage is extracted BEFORE status/domain validation so an
            # incurred attempt is accounted even when it failed or the
            # payload is malformed. Unparseable bodies keep usage unknown.
            data: Any = None
            body_is_json = True
            try:
                data = resp.json()
            except ValueError:
                body_is_json = False
            usage = data.get("usage") if isinstance(data, dict) else None
            if not isinstance(usage, dict):
                usage = {}
            request_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(request_id, str) or not request_id:
                request_id = resp.headers.get("x-request-id")
            record = LLMCallRecord(
                purpose=purpose, model=str(payload["model"]),
                transport_attempt=transport_attempt,
                request_id=request_id,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=_token_count(usage.get("input_tokens")),
                output_tokens=_token_count(usage.get("output_tokens")),
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                if tracker is not None:
                    account_before_retry(record)
                else:
                    attempt_records.append(record)
                time.sleep(self._settings.llm_backoff_seconds * (2 ** attempt))
                continue
            if tracker is not None:
                # Terminal path: failed attempts are recorded here (the
                # caller must not record them again); a successful record
                # is returned for the caller to record exactly once.
                def fail(failed_record: LLMCallRecord, message: str,
                         cause: Exception | None = None) -> Any:
                    if logical_attempt:
                        failed_record.attempt = logical_attempt
                    tracker.record(failed_record)
                    error = LLMResponseError(message)
                    if cause is not None:
                        raise error from cause
                    raise error

                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    fail(record, f"Live model call failed: HTTP {resp.status_code}", exc)
                if not body_is_json or not isinstance(data, dict):
                    fail(record, "Malformed live response: envelope was not a JSON object")
                try:
                    parsed = json.loads(_extract_text(data))
                except (json.JSONDecodeError, LLMResponseError) as exc:
                    fail(record, f"Malformed live response: {exc}", exc)
                if not isinstance(parsed, dict):
                    fail(record, "Live response JSON was not an object")
                if logical_attempt:
                    record.attempt = logical_attempt
                return parsed, record
            attempt_records.append(record)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise _attempt_error(
                    f"Live model call failed: HTTP {resp.status_code}",
                    attempt_records,
                ) from exc
            if not body_is_json or not isinstance(data, dict):
                raise _attempt_error(
                    "Malformed live response: envelope was not a JSON object",
                    attempt_records,
                )
            try:
                parsed = json.loads(_extract_text(data))
            except (json.JSONDecodeError, LLMResponseError) as exc:
                raise _attempt_error(
                    f"Malformed live response: {exc}", attempt_records
                ) from exc
            if not isinstance(parsed, dict):
                raise _attempt_error(
                    "Live response JSON was not an object", attempt_records
                )
            record.prior_attempts = list(attempt_records[:-1])
            return parsed, record
        raise _attempt_error("Live model call failed", attempt_records)

    def propose_plan(self, goal_title, description, repair_feedback=None,
                     tracker=None, retry_reserve_tokens=0):
        prompt = (
            "Plan synthetic-data tool steps as JSON for this goal. "
            f"Goal: {goal_title}. Details: {description}. {TOOLS_HINT} "
            "Accounts: acc_alice, acc_bob, acc_ops. Currency USD. "
            "Every step must set every field: use account_id for "
            "get_balance/list_transactions; from_account, to_account, amount "
            "and currency for simulate_transfer; limit only for "
            "list_transactions (integer 1-100, use 1 elsewhere); empty "
            "string or 0 elsewhere. "
            'Example: {"tool_name": "simulate_transfer", "account_id": "", '
            '"from_account": "acc_alice", "to_account": "acc_bob", '
            '"amount": 100, "currency": "USD", "rationale": "pay"}'
        )
        if repair_feedback:
            prompt += f" Previous plan was rejected: {repair_feedback}. Fix it."
        parsed, record = self._call("plan_steps", PLAN_SCHEMA, prompt,
                                    "repair" if repair_feedback else "plan",
                                    tracker=tracker,
                                    retry_reserve_tokens=retry_reserve_tokens)
        steps = parsed.get("steps")
        if not isinstance(steps, list):
            # Incurred usage travels with the error so it is never lost.
            raise LLMResponseError(
                "Live plan response missing steps list", record=record
            )
        return steps, record

    def semantic_review(self, payload, tracker=None, retry_reserve_tokens=0):
        prompt = (
            "Independently review whether the executed synthetic workflow satisfies "
            "the user goal. Judge only from the supplied goal, approved plan and "
            "independently obtained ledger evidence. Return the review JSON.\n"
            + json.dumps(payload, default=str)[:6000]
        )
        return self._call("semantic_review", REVIEW_SCHEMA, prompt,
                          "semantic_review", model=self._settings.review_model,
                          tracker=tracker,
                          retry_reserve_tokens=retry_reserve_tokens)


def build_provider(settings: Settings) -> LLMProvider:
    """Single explicit switch. live -> Meta (raises without key), else Fake."""
    if settings.llm_mode == "live":
        if not settings.model_api_key:
            raise LLMNotConfiguredError("TRUSTFLOW_LLM_MODE=live requires MODEL_API_KEY")
        return MetaResponsesProvider(settings)
    return FakeLLMProvider()
