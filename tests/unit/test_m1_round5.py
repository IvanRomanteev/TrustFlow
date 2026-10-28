"""Round-5 regressions: independent offline audit F1-F5 on 89c1812.

F1 malformed provider envelope must not strand a committed transfer in
VERIFYING; F2 every HTTP attempt is accounted; F3 revision bounds;
F4 provider/local schema parity; F5 audit keeps the semantic outage.
"""

import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trustflow.agents.planner import validate_steps
from trustflow.agents.semantic import SemanticReviewInput
from trustflow.api.app import Container, create_app
from trustflow.core.errors import LLMResponseError, PlanValidationError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import (
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    FakeLLMProvider,
    MetaResponsesProvider,
)
from trustflow.llm.usage import LLMCallRecord

SECRET = "OFFLINE-SECRET-R5"


def transfer(amount=73):
    return dict(tool_name="simulate_transfer", from_account="acc_alice",
                to_account="acc_bob", amount=amount, currency="USD")


READ = dict(tool_name="get_balance", account_id="acc_alice")


class Sequence(FakeLLMProvider):
    def __init__(self, plans, fail_reviews=0):
        super().__init__()
        self.plans, self.plan_calls, self.review_calls = plans, 0, 0
        self.fail_reviews = fail_reviews

    def propose_plan(self, title, description, repair_feedback=None):
        index = min(self.plan_calls, len(self.plans) - 1)
        self.plan_calls += 1
        return copy.deepcopy(self.plans[index]), LLMCallRecord(
            model="fake", purpose="repair" if repair_feedback else "plan")

    def semantic_review(self, payload):
        self.review_calls += 1
        if self.review_calls <= self.fail_reviews:
            raise LLMResponseError(f"injected semantic outage; Authorization {SECRET}")
        return super().semantic_review(payload)


def box(plans=None, fail_reviews=0, **settings):
    c = Container(Settings(_env_file=None, llm_mode="fake", **settings))
    p = Sequence(plans or [[transfer()]], fail_reviews=fail_reviews)
    c.engine.planner._llm = p
    c.engine.semantic._llm = p
    return c, p


def amounts(c):
    return [t.amount for t in c.registry.ledger.transactions]


def meta(handler, **settings):
    return MetaResponsesProvider(
        Settings(_env_file=None, llm_api_key="offline-dummy", **settings),
        transport=httpx.MockTransport(handler),
    )


# --- F1: malformed envelope after a committed transfer ---

BAD_ENVELOPES = [
    [],
    {"usage": [], "output": [None]},
    {"usage": {"input_tokens": "invalid"}, "output_text": "{}"},
]


@pytest.mark.parametrize("bad", BAD_ENVELOPES)
def test_f1_malformed_envelope_recovers_after_commit(bad):
    c, _ = box()
    c.engine.semantic._llm = meta(
        lambda request: httpx.Response(200, json=bad), llm_max_retries=0)
    with TestClient(create_app(c), raise_server_exceptions=False) as client:
        response = client.post("/workflows", json={"title": "payment"})
        run = c.repo.list()[0]
        assert response.status_code == 201
        assert run.status == RunStatus.VERIFICATION_PENDING
        assert amounts(c) == [73]
        # Public recovery endpoints never leave the run stuck in VERIFYING.
        retry = client.post(f"/workflows/{run.run_id}/verify-retry").json()
        reconcile = client.post(f"/workflows/{run.run_id}/reconcile").json()
        assert retry["status"] != "VERIFYING"
        assert reconcile["status"] != "VERIFYING"
        assert amounts(c) == [73]


@pytest.mark.parametrize("bad", BAD_ENVELOPES)
def test_f1_malformed_envelope_is_llm_response_error_with_usage(bad):
    # Provider envelope errors and semantic-schema errors both surface as
    # LLMResponseError (the latter via SemanticReviewer validation).
    from trustflow.agents.semantic import SemanticReviewer
    from trustflow.domain.models import Plan
    from trustflow.tools.registry import ToolRegistry

    provider = meta(lambda request: httpx.Response(200, json=bad), llm_max_retries=0)
    reviewer = SemanticReviewer(provider, ToolRegistry().ledger)
    with pytest.raises(LLMResponseError) as caught:
        reviewer.review(WorkflowGoal(title="x"), Plan(goal_title="x", steps=[]))
    record = caught.value.record
    assert record is not None
    # Invalid/absent token values are unknown, never fabricated numbers.
    assert record.input_tokens is None and record.output_tokens is None


async def test_f1_verify_retry_completes_once_envelope_is_healthy():
    c, _ = box()
    c.engine.semantic._llm = meta(
        lambda request: httpx.Response(200, json=[]), llm_max_retries=0)
    run = await c.engine.create_workflow(WorkflowGoal(title="payment"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    c.engine.semantic._llm = FakeLLMProvider()
    run = await c.engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED
    assert amounts(c) == [73]


# --- F2: usage accounting for every HTTP attempt ---

@pytest.mark.parametrize("first", [503, "transport"])
async def test_f2_each_http_attempt_has_usage_record(first):
    c, _ = box()
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            if first == "transport":
                raise httpx.ReadTimeout("injected read timeout", request=request)
            return httpx.Response(503, json={
                "id": "failed-attempt",
                "usage": {"input_tokens": 17, "output_tokens": 3}})
        return httpx.Response(200, json={
            "id": "second-attempt",
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "output_text": json.dumps({"steps": [READ]})})

    c.engine.planner._llm = meta(handler, llm_max_retries=1, llm_backoff_seconds=0)
    run = await c.engine.create_workflow(WorkflowGoal(title="read balance"))
    records = [r for r in run.llm_calls if r["purpose"] == "plan"]
    assert len(records) == len(calls) == 2
    # Logical-call attempt is shared; transport attempts are distinct.
    assert records[0]["attempt"] == records[1]["attempt"]
    assert [r["transport_attempt"] for r in records] == [1, 2]
    assert records[1]["request_id"] == "second-attempt"
    assert (records[1]["input_tokens"], records[1]["output_tokens"]) == (5, 2)
    if first == "transport":
        assert records[0]["usage_unknown"] is True
        assert records[0]["input_tokens"] is None
        assert records[0]["output_tokens"] is None
    else:
        assert records[0]["request_id"] == "failed-attempt"
        assert (records[0]["input_tokens"], records[0]["output_tokens"]) == (17, 3)
        assert records[0]["usage_unknown"] is False
        # Retry tokens count toward the workflow total (no lost attempt).
        total = sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                    for r in records)
        assert total == 17 + 3 + 5 + 2


async def test_f2_terminal_http_failure_records_unknown_usage():
    c, _ = box()
    c.engine.planner._llm = meta(
        lambda request: httpx.Response(503, json={"error": "injected outage"}),
        llm_max_retries=0)
    run = await c.engine.create_workflow(WorkflowGoal(title="read balance"))
    assert run.status == RunStatus.FAILED
    assert len(run.llm_calls) == 1
    assert run.llm_calls[0]["usage_unknown"] is True
    assert run.llm_calls[0]["input_tokens"] is None
    assert run.llm_calls[0]["transport_attempt"] == 1


async def test_f2_terminal_transport_failure_records_unknown_usage():
    c, _ = box()

    def handler(request):
        raise httpx.ReadTimeout("injected read timeout", request=request)

    c.engine.planner._llm = meta(handler, llm_max_retries=0)
    run = await c.engine.create_workflow(WorkflowGoal(title="read balance"))
    assert run.status == RunStatus.FAILED
    assert len(run.llm_calls) == 1
    assert run.llm_calls[0]["usage_unknown"] is True


# --- F3: bounds apply to every revision ---

async def test_f3_revision_obeys_max_steps():
    c, _ = box([[transfer()], [READ, READ, transfer()]], max_steps=2)
    c.registry.ledger.fail_next_transfer = True
    run = await c.engine.create_workflow(WorkflowGoal(title="payment"))
    assert run.status != RunStatus.COMPLETED
    assert run.status == RunStatus.FAILED
    # The oversized revision was rejected before executing any of its steps.
    assert amounts(c) == []


# --- F4: provider/local schema parity ---

def test_f4_semantic_required_fields_match_provider_contract():
    assert set(REVIEW_SCHEMA["required"]) == {
        "verdict", "confidence", "issues", "summary"}
    for missing in ({"verdict": "PASS", "confidence": 1.0},
                    {"verdict": "PASS", "confidence": 1.0, "issues": []},
                    {"verdict": "PASS", "confidence": 1.0, "summary": "ok"}):
        with pytest.raises(ValidationError):
            SemanticReviewInput.model_validate(missing)


def test_f4_semantic_wrong_types_rejected():
    base = {"verdict": "PASS", "confidence": 1.0, "issues": [], "summary": "ok"}
    for patch in ({"confidence": "high"}, {"issues": "not-a-list"},
                  {"verdict": "MAYBE"}, {"confidence": 1.5}, {"summary": 7}):
        with pytest.raises(ValidationError):
            SemanticReviewInput.model_validate({**base, **patch})


async def test_f4_incomplete_review_is_not_completed():
    c, _ = box()
    body = {"id": "incomplete-review",
            "output_text": json.dumps({"verdict": "PASS", "confidence": 1.0})}
    c.engine.semantic._llm = meta(
        lambda request: httpx.Response(200, json=body), llm_max_retries=0)
    run = await c.engine.create_workflow(WorkflowGoal(title="payment"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert amounts(c) == [73]


@pytest.mark.parametrize("limit,ok", [(0, False), (1, True), (100, True), (101, False)])
def test_f4_list_transactions_limit_bounds(limit, ok):
    step = dict(tool_name="list_transactions", account_id="acc_alice", limit=limit)
    if ok:
        parsed = validate_steps([step])
        assert parsed[0].args["limit"] == limit
    else:
        with pytest.raises(PlanValidationError):
            validate_steps([step])
    # The flat provider schema expresses the same bounds.
    limit_schema = PLAN_SCHEMA["properties"]["steps"]["items"]["properties"]["limit"]
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 100


def test_f4_plan_wrong_types_rejected():
    with pytest.raises(PlanValidationError):
        validate_steps([dict(tool_name="list_transactions",
                             account_id="acc_alice", limit="not-a-number")])
    with pytest.raises(PlanValidationError):
        validate_steps([dict(tool_name="list_transactions", limit=5)])


# --- F5: audit retains the semantic outage after recovery ---

async def test_f5_recovery_audit_retains_outage():
    c, _ = box(fail_reviews=1)
    run = await c.engine.create_workflow(WorkflowGoal(title="payment"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    await c.engine.retry_verification(run)
    assert run.status == RunStatus.COMPLETED and run.error is None
    events = [e.model_dump(mode="json") for e in c.audit.list(run.run_id)]
    assert any("outage" in json.dumps(e) or "unavailable" in json.dumps(e)
               for e in events)
    failures = [e for e in events if e["action"] == "semantic_review_failed"]
    assert failures
    assert failures[0]["detail"]["attempt"] == 1
    assert failures[0]["detail"]["recovery_state"] == "VERIFICATION_PENDING"
    assert SECRET not in json.dumps(events)
    assert amounts(c) == [73]
