"""Round-6 regressions: residual findings R1-R3 from the 71df99c re-review.

R1 extreme token counts must not overflow cost accounting or strand a
committed transfer; R2 the workflow budget/reservation is re-checked
inside the transport retry loop against the originating workflow's
tracker; R3 external JSON numeric fields reject bool/string coercion.
"""

import copy
import json
import math

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trustflow.agents.planner import Planner, validate_steps
from trustflow.agents.semantic import SemanticReviewer, SemanticReviewInput
from trustflow.api.app import Container, create_app
from trustflow.core.errors import PlanValidationError, TokenBudgetExceededError
from trustflow.core.settings import Settings
from trustflow.domain.models import Plan, RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider, MetaResponsesProvider
from trustflow.llm.usage import LLMCallRecord, UsageTracker, estimate_cost
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry

PASS = {"verdict": "PASS", "confidence": 0.8, "issues": [], "summary": "ok"}


def transfer(amount=73):
    return dict(tool_name="simulate_transfer", from_account="acc_alice",
                to_account="acc_bob", amount=amount, currency="USD")


READ = dict(tool_name="get_balance", account_id="acc_alice")


class Sequence(FakeLLMProvider):
    def __init__(self, plans):
        super().__init__()
        self.plans, self.plan_calls = plans, 0

    def propose_plan(self, title, description, repair_feedback=None):
        index = min(self.plan_calls, len(self.plans) - 1)
        self.plan_calls += 1
        return copy.deepcopy(self.plans[index]), LLMCallRecord(
            model="fake", purpose="repair" if repair_feedback else "plan")


def box(plans=None, **settings):
    c = Container(Settings(_env_file=None, llm_mode="fake", **settings))
    p = Sequence(plans or [[transfer()]])
    c.engine.planner._llm = p
    c.engine.semantic._llm = p
    return c, p


def amounts(c):
    return [t.amount for t in c.registry.ledger.transactions]


def meta(handler, **settings):
    return MetaResponsesProvider(
        Settings(_env_file=None, llm_api_key="offline-dummy",
                 llm_backoff_seconds=0, **settings),
        transport=httpx.MockTransport(handler),
    )


def body(content, usage=None, rid="r6"):
    result = {"id": rid, "output_text": json.dumps(content)}
    if usage is not None:
        result["usage"] = usage
    return result


# --- R1: extreme token counts ---

def test_r1_extreme_usage_does_not_strand_committed_transfer():
    c, _ = box()
    c.engine.semantic._llm = meta(
        lambda request: httpx.Response(
            200, json=body(PASS, {"input_tokens": 10**400, "output_tokens": 11})),
        llm_max_retries=0)
    with TestClient(create_app(c), raise_server_exceptions=False) as client:
        response = client.post("/workflows", json={"title": "payment"})
        assert response.status_code == 201
        run = c.repo.list()[0]
        assert run.status != RunStatus.VERIFYING
        assert amounts(c) == [73]
        # The known part of the usage survived; the unusable count is
        # explicit unknown, never replaced by an invented smaller number.
        semantic = [r for r in run.llm_calls if r["purpose"] == "semantic_review"]
        assert semantic and semantic[-1]["output_tokens"] == 11
        assert semantic[-1]["input_tokens"] is None
        assert semantic[-1]["usage_unknown"] is True
        # Public recovery endpoints answer in a defined state.
        retry = client.post(f"/workflows/{run.run_id}/verify-retry").json()
        assert retry["status"] != "VERIFYING"
        assert amounts(c) == [73]
        audit = client.get(f"/workflows/{run.run_id}/audit")
        assert audit.status_code == 200


def test_r1_cost_estimation_and_recording_survive_extreme_counts():
    # Direct recording of an extreme count (bypassing provider ingestion)
    # must keep the record and must not raise OverflowError.
    tracker = UsageTracker(budget_tokens=10**401)
    record = LLMCallRecord(purpose="plan", model="muse-wonder-1.0",
                           input_tokens=10**400, output_tokens=11)
    tracker.record(record)
    assert tracker.records == [record]
    assert isinstance(estimate_cost("muse-wonder-1.0", 10**400, 11), float)
    assert estimate_cost("muse-wonder-1.0", 1_000_000, 0) == 4.0
    assert math.isfinite(estimate_cost("fake", 5, 7))


def test_r1_extreme_count_in_plan_response_is_accounted_unknown():
    provider = meta(
        lambda request: httpx.Response(
            200, json=body({"steps": [READ]},
                           {"input_tokens": 10**400, "output_tokens": 11})),
        llm_max_retries=0)
    steps, record = provider.propose_plan("read", "")
    assert steps == [READ]
    assert record.input_tokens is None and record.output_tokens == 11


# --- R2: budget/reservation inside the transport retry loop ---

@pytest.mark.parametrize("first_tokens", [2000, 100])
async def test_r2_budget_blocks_second_transport_request(first_tokens):
    # budget 1500; the planner reservation for title 'read' is 1457, so
    # even 100 known tokens from attempt 1 leave no room for a retry.
    c, _ = box([[READ]], token_budget_per_workflow=1500)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={
                "id": "spent",
                "usage": {"input_tokens": first_tokens, "output_tokens": 0}})
        return httpx.Response(200, json=body({"steps": [READ]},
                                             {"input_tokens": 5, "output_tokens": 2}))

    c.engine.planner._llm = meta(handler, llm_max_retries=1)
    run = await c.engine.create_workflow(WorkflowGoal(title="read"))
    assert len(calls) == 1
    assert run.status == RunStatus.FAILED
    # The incurred first attempt is preserved, not lost to the rejection.
    records = [r for r in run.llm_calls if r["purpose"] == "plan"]
    assert len(records) == 1
    assert records[0]["input_tokens"] == first_tokens
    assert records[0]["transport_attempt"] == 1


async def test_r2_sufficient_budget_retries_and_accounts_each_attempt_once():
    c, _ = box([[READ]])
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={
                "id": "first", "usage": {"input_tokens": 17, "output_tokens": 3}})
        return httpx.Response(200, json=body({"steps": [READ]},
                                             {"input_tokens": 5, "output_tokens": 2},
                                             "second"))

    c.engine.planner._llm = meta(handler, llm_max_retries=1)
    run = await c.engine.create_workflow(WorkflowGoal(title="read"))
    assert run.status == RunStatus.COMPLETED
    assert len(calls) == 2
    records = [r for r in run.llm_calls if r["purpose"] == "plan"]
    assert len(records) == 2
    assert [r["transport_attempt"] for r in records] == [1, 2]
    assert records[0]["attempt"] == records[1]["attempt"]
    assert sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0)
               for r in records) == 27


async def test_r2_semantic_retry_is_budget_checked_before_next_request():
    registry = ToolRegistry()
    tracker = UsageTracker(budget_tokens=1400, workflow_id="semantic-r2")
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={
                "id": "first", "usage": {"input_tokens": 100, "output_tokens": 0}})
        return httpx.Response(200, json=body(PASS,
                                             {"input_tokens": 5, "output_tokens": 2}))

    reviewer = SemanticReviewer(meta(handler, llm_max_retries=1),
                                registry.ledger, max_output_tokens=1200)
    with pytest.raises(TokenBudgetExceededError):
        reviewer.review(WorkflowGoal(title="pay"),
                        Plan(goal_title="pay", steps=[]), tracker=tracker)
    assert len(calls) == 1
    assert len(tracker.records) == 1
    assert tracker.records[0].input_tokens == 100
    assert tracker.records[0].transport_attempt == 1


async def test_r2_repair_call_retries_are_accounted_in_originating_workflow():
    # First logical call returns an invalid plan (repair follows); the
    # repair call's first transport attempt is a 503 with known usage.
    tracker = UsageTracker(budget_tokens=20000, workflow_id="repair-r2")
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json=body(
                {"steps": [{"tool_name": "nope"}]},
                {"input_tokens": 3, "output_tokens": 1}))
        if len(calls) == 2:
            return httpx.Response(503, json={
                "id": "repair-first",
                "usage": {"input_tokens": 17, "output_tokens": 3}})
        return httpx.Response(200, json=body({"steps": [READ]},
                                             {"input_tokens": 5, "output_tokens": 2}))

    planner = Planner(meta(handler, llm_max_retries=1), PolicyEngine(Settings()))
    plan = planner.create_plan(WorkflowGoal(title="read"), tracker=tracker)
    assert [s.tool_name for s in plan.steps] == ["get_balance"]
    assert len(calls) == 3
    repair = [r for r in tracker.records if r.purpose == "repair"]
    assert len(repair) == 2
    assert [r.transport_attempt for r in repair] == [1, 2]
    assert repair[0].attempt == repair[1].attempt
    assert all(r.workflow_id == "repair-r2" for r in tracker.records)


async def test_r2_terminal_failure_after_retry_records_all_attempts_once():
    c, _ = box([[READ]])
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={
                "id": "first", "usage": {"input_tokens": 17, "output_tokens": 3}})
        return httpx.Response(503, json={"error": "still down"})

    c.engine.planner._llm = meta(handler, llm_max_retries=1)
    run = await c.engine.create_workflow(WorkflowGoal(title="read"))
    assert run.status == RunStatus.FAILED
    records = [r for r in run.llm_calls if r["purpose"] == "plan"]
    assert len(records) == len(calls) == 2
    assert records[0]["input_tokens"] == 17
    assert records[1]["usage_unknown"] is True


# --- R3: strict JSON numeric types ---

@pytest.mark.parametrize("bad_limit", [True, "1", "100", 1.5, "invalid"])
def test_r3_limit_rejects_bool_string_and_fraction(bad_limit):
    with pytest.raises(PlanValidationError):
        validate_steps([dict(tool_name="list_transactions",
                             account_id="acc_alice", limit=bad_limit)])


@pytest.mark.parametrize("good_limit", [1, 100, 20])
def test_r3_limit_accepts_valid_integers(good_limit):
    parsed = validate_steps([dict(tool_name="list_transactions",
                                  account_id="acc_alice", limit=good_limit)])
    assert parsed[0].args["limit"] == good_limit


def test_r3_limit_accepts_integer_valued_json_number():
    # JSON Schema "integer" also matches numbers with zero fraction.
    parsed = validate_steps([dict(tool_name="list_transactions",
                                  account_id="acc_alice", limit=1.0)])
    assert parsed[0].args["limit"] == 1
    assert isinstance(parsed[0].args["limit"], int)


@pytest.mark.parametrize("bad_confidence", [True, "0.8"])
def test_r3_confidence_rejects_bool_and_string(bad_confidence):
    with pytest.raises(ValidationError):
        SemanticReviewInput.model_validate({**PASS, "confidence": bad_confidence})


@pytest.mark.parametrize("good_confidence", [0, 1, 0.8])
def test_r3_confidence_accepts_valid_numbers(good_confidence):
    parsed = SemanticReviewInput.model_validate(
        {**PASS, "confidence": good_confidence})
    assert parsed.confidence == float(good_confidence)


@pytest.mark.parametrize("bad_confidence", [True, "0.8"])
async def test_r3_forbidden_confidence_type_cannot_complete(bad_confidence):
    c, _ = box()
    c.engine.semantic._llm = meta(
        lambda request: httpx.Response(
            200, json=body({**PASS, "confidence": bad_confidence})),
        llm_max_retries=0)
    run = await c.engine.create_workflow(WorkflowGoal(title="payment"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert amounts(c) == [73]


@pytest.mark.parametrize("bad_amount", [True, "100"])
def test_r3_transfer_amount_rejects_bool_and_string(bad_amount):
    with pytest.raises(PlanValidationError):
        validate_steps([dict(tool_name="simulate_transfer",
                             from_account="acc_alice", to_account="acc_bob",
                             amount=bad_amount, currency="USD")])


def test_r3_missing_semantic_fields_still_rejected():
    for field in ("verdict", "confidence", "issues", "summary"):
        incomplete = {k: v for k, v in PASS.items() if k != field}
        with pytest.raises(ValidationError):
            SemanticReviewInput.model_validate(incomplete)
    with pytest.raises(ValidationError):
        SemanticReviewInput.model_validate({**PASS, "confidence": 1.5})
