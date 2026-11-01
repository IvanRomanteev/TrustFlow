"""Current Meta wire contract and safety boundaries. No network or real keys."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.core.errors import (
    LiveBudgetExceededError,
    LLMNotConfiguredError,
    LLMResponseError,
    PlanValidationError,
    TokenBudgetExceededError,
)
from trustflow.core.logging import redact
from trustflow.core.settings import Settings
from trustflow.domain.models import WorkflowGoal
from trustflow.llm.provider import (
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    MetaResponsesProvider,
    _validate_schema,
    build_provider,
)
from trustflow.llm.usage import LiveCallBudget, LLMCallRecord, UsageTracker, estimate_cost
from trustflow.tools.ledger import SyntheticLedger

PASS = {"verdict": "PASS", "confidence": 0.9, "issues": [], "summary": "Evidence agrees"}
READ = {"tool_name": "get_balance", "account_id": "acc_alice"}


def envelope(content, model="muse-spark-1.3", **patch):
    return {
        "id": "resp_offline_1", "object": "response", "status": "completed", "model": model,
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": json.dumps(content)}]},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50}, **patch,
    }


def meta(handler, **settings):
    defaults = {"llm_mode": "live", "planning_model": "muse-spark-1.3",
                "review_model": "muse-wonder-1.0", "llm_max_retries": 0,
                "llm_backoff_seconds": 0}
    return MetaResponsesProvider(Settings(_env_file=None, **(defaults | settings)),
                                 transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("model", ["muse-spark-1.3", "muse-wonder-1.0"])
def test_current_wire_plan_and_review_models(model):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url == "https://api.meta.ai/v1/responses"
        assert body["model"] == model
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert "tools" not in body  # model cannot execute tools server-side
        assert body["max_output_tokens"] == 1200
        content = {"steps": [READ]} if len(calls) == 1 else PASS
        return httpx.Response(200, json=envelope(content, model),
                              headers={"x-request-id": "req_offline_1"})

    provider = meta(handle, planning_model=model, review_model=model)
    tracker = UsageTracker(20000, "workflow-a")
    plan = Planner(provider).create_plan(WorkflowGoal(title="Read balance"), tracker=tracker)
    review = SemanticReviewer(provider, SyntheticLedger()).review(
        WorkflowGoal(title="Read balance"), plan, tracker=tracker)
    assert review.verdict == "PASS"
    assert [r.validation_outcome for r in tracker.records] == [
        "typed_plan_valid", "typed_review_valid"]
    assert all(r.workflow_id == "workflow-a" and r.model == model for r in tracker.records)
    assert all(r.request_id == "req_offline_1" and r.response_id == "resp_offline_1"
               for r in tracker.records)
    assert tracker.total_tokens == 300
    assert tracker.total_cost_usd == round(2 * estimate_cost(model, 100, 50), 8)
    assert all(r.latency_ms >= 0 and r.http_status == 200 for r in tracker.records)


@pytest.mark.parametrize("patch", [
    {"status": "incomplete"}, {"status": "failed"}, {"status": "queued"},
    {"model": "muse-other"}, {"error": {"message": "private body"}},
    {"output": [{"type": "reasoning", "content": [{"text": '{"steps": []}'}]}]},
    {"output": [{"type": "function_call", "arguments": '{"steps": []}'}]},
    {"output": [{"type": "message", "role": "assistant", "status": "incomplete",
                 "content": [{"type": "output_text", "text": '{"steps": []}'}]}]},
    {"output": [{"type": "message", "role": "assistant", "content": [
        {"type": "refusal", "refusal": "private body"}]}]},
    {"output": None}, {"output": "not a list"},
])
def test_invalid_envelopes_fail_closed_and_preserve_usage(patch):
    p = meta(lambda r: httpx.Response(200, json=envelope({"steps": [READ]}, **patch)))
    tracker = UsageTracker(20000, "bad-envelope")
    with pytest.raises(LLMResponseError):
        Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=tracker)
    assert len(tracker.records) == p.budget.requests == 1
    assert tracker.total_tokens == 150
    assert tracker.records[0].validation_outcome == "failed"


@pytest.mark.parametrize("text", ["[]", "null", "not JSON", '{"steps":NaN}',
                                       '{"steps":[],"steps":[{}]}'])
def test_invalid_json_is_terminal(text):
    body = envelope({})
    body["output"][1]["content"][0]["text"] = text
    p = meta(lambda r: httpx.Response(200, json=body), llm_max_retries=2)
    with pytest.raises(LLMResponseError) as error:
        p.propose_plan("Read", "")
    assert p.budget.requests == 1
    assert error.value.record.input_tokens == 100


@pytest.mark.parametrize("status,retried", [(400, False), (401, False), (402, False),
    (403, False), (404, False), (408, False), (413, False), (429, True), (500, True),
    (502, True), (503, True), (504, True)])
def test_http_failures_bounded_and_accounted(status, retried):
    p = meta(lambda r: httpx.Response(status, json={"error": {"message": "private body"}}),
             llm_max_retries=2)
    tracker = UsageTracker(20000)
    with pytest.raises(LLMResponseError, match=f"HTTP {status}"):
        Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=tracker)
    assert len(tracker.records) == p.budget.requests == (3 if retried else 1)
    assert all(r.usage_unknown and r.failure_reason == f"http_{status}"
               for r in tracker.records)
    assert p.budget.charged_usd > 0  # unknown usage cannot make attempts free


def test_retry_after_and_timeout_redaction(monkeypatch):
    sleeps = []
    monkeypatch.setattr("trustflow.llm.provider.time.sleep", sleeps.append)
    p = meta(lambda r: httpx.Response(429, headers={"Retry-After": "60"}), llm_max_retries=2)
    with pytest.raises(LLMResponseError):
        p.propose_plan("Read", "")
    assert p.budget.requests == 1 and sleeps == []
    calls = []

    def handle(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        raise httpx.ReadTimeout("credential should not appear", request=request)

    p = meta(handle, llm_max_retries=1)
    with pytest.raises(LLMResponseError) as error:
        p.propose_plan("Read", "")
    assert "credential" not in str(error.value)
    assert error.value.__cause__ is None
    assert len(error.value.records) == 2 and 2 <= sleeps[0] <= 2.05
    assert error.value.record.prior_attempts[0].http_status == 429


@pytest.mark.parametrize("bad", [[{"tool_name": "shell", "command": "pay"}],
    [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
      "to_account": "acc_bob", "amount": -1}],
    [{"tool_name": "get_balance", "account_id": "foreign"}],
    [{"tool_name": "list_transactions", "account_id": "acc_alice", "limit": True}]])
def test_invalid_plan_repair_once_and_never_execute(bad):
    p = meta(lambda r: httpx.Response(200, json=envelope({"steps": bad})))
    tracker = UsageTracker(20000)
    with pytest.raises(PlanValidationError):
        Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=tracker)
    assert p.budget.requests == 2
    assert [r.purpose for r in tracker.records] == ["plan", "repair"]
    assert all(r.validation_outcome == "typed_plan_invalid" for r in tracker.records)


def test_valid_repair_then_token_budget_blocks_http_retry():
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(200, json=envelope({"steps": [READ] if len(calls) > 1 else []}))

    p = meta(handle)
    tracker = UsageTracker(20000)
    assert Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=tracker).steps
    assert [r.validation_outcome for r in tracker.records] == [
        "typed_plan_invalid", "typed_plan_valid"]
    p = meta(lambda r: httpx.Response(503, json={
        "usage": {"input_tokens": 900, "output_tokens": 0}}), llm_max_retries=2)
    tracker = UsageTracker(2000, "limited")
    with pytest.raises(TokenBudgetExceededError):
        Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=tracker)
    assert p.budget.requests == 1 and tracker.total_tokens == 900


def test_cost_and_request_caps_hold_under_concurrency():
    budget = LiveCallBudget(3, 1)

    def acquire(_):
        try:
            budget.acquire("muse-wonder-1.0", 100, 1200)
            return True
        except LiveBudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(acquire, range(20))) == budget.requests == 3
    budget = LiveCallBudget(20, 0.03)
    reserved = budget.acquire("muse-wonder-1.0", 100, 1200)
    rec = LLMCallRecord(purpose="plan", model="muse-wonder-1.0")
    budget.settle(reserved, rec)
    with pytest.raises(LiveBudgetExceededError):
        budget.acquire("muse-wonder-1.0", 100, 1200)
    assert budget.requests == 1 and rec.usage_unknown


def test_budget_block_retains_prior_attempt_and_workflow_isolation():
    p = meta(lambda r: httpx.Response(503), llm_max_retries=2, llm_max_requests=1)
    a, b = UsageTracker(20000, "a"), UsageTracker(20000, "b")
    with pytest.raises(LLMResponseError):
        Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=a)
    with pytest.raises(LLMResponseError):
        Planner(p).create_plan(WorkflowGoal(title="Read"), tracker=b)
    assert len(a.records) == 1 and not b.records
    assert a.records[0].workflow_id == "a"


def test_strict_schema_validation_before_http():
    _validate_schema(PLAN_SCHEMA)
    _validate_schema(REVIEW_SCHEMA)
    p = meta(lambda r: pytest.fail("Invalid schema must not reach HTTP"))
    for schema in ({"type": "object"}, {"type": "array"},
                   {"type": "object", "additionalProperties": False, "required": None},
                   {"type": "object", "$ref": "#"}):
        with pytest.raises(LLMResponseError):
            p._call("bad", schema, "Read", "plan")
    assert p.budget.requests == 0


def test_spending_blocks_before_http_and_reported_usage_can_overshoot():
    p = meta(lambda r: pytest.fail("Budget must block before HTTP"), llm_max_cost_usd=0.00001)
    with pytest.raises(LiveBudgetExceededError):
        p.propose_plan("Read", "")
    assert p.budget.requests == 0
    budget = LiveCallBudget(5, 0.03)
    reserved = budget.acquire("muse-wonder-1.0", 100, 1200)
    record = LLMCallRecord(purpose="plan", model="muse-wonder-1.0",
                           input_tokens=50000, output_tokens=10000)
    budget.settle(reserved, record)
    assert budget.charged_usd == pytest.approx(0.4)
    with pytest.raises(LiveBudgetExceededError):
        budget.acquire("muse-wonder-1.0", 100, 1200)


def test_offline_transport_always_uses_placeholder_auth():
    def handle(request):
        assert request.headers["authorization"] == "Bearer offline-transport-placeholder"
        return httpx.Response(200, json=envelope({"steps": [READ]}))
    meta(handle).propose_plan("Read", "")


@pytest.mark.parametrize("patch", [{"planning_model": "muse-wonder-full-1.0"},
    {"review_model": "muse-spark-1.3-contributor"}, {"llm_max_retries": -1},
    {"llm_max_retries": 4}, {"max_output_tokens": 15}, {"llm_timeout_seconds": 0},
    {"llm_max_cost_usd": float("nan")}, {"llm_max_cost_usd": float("inf")}])
def test_live_settings_invalid_values(patch):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_mode="live", **patch)


def test_only_environment_model_api_key_and_sanitized_metadata(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY")
    monkeypatch.setenv("META_API_KEY", "ignored-offline")
    s = Settings(_env_file=None, llm_mode="live", llm_api_key="ignored-legacy")
    assert "ignored-legacy" not in repr(s) and "llm_api_key" not in s.model_dump()
    with pytest.raises(LLMNotConfiguredError):
        build_provider(s)
    monkeypatch.setenv("MODEL_API_KEY", "offline-opaque-value")
    assert "offline-opaque-value" not in redact("rationale offline-opaque-value")
    p = meta(lambda r: httpx.Response(200, json=envelope({"steps": [READ]},
        id="offline-opaque-value"), headers={"x-request-id": "Bearer private-value"}))
    _, record = p.propose_plan("Read", "")
    assert record.request_id is record.response_id is None
    with pytest.raises(LLMNotConfiguredError):
        MetaResponsesProvider(Settings(_env_file=None, llm_base_url="http://other.invalid"))
