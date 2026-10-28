"""N1 regression: huge JSON integers must not overflow strict_json_number.

A semantic PASS with confidence=+-10**400 after a committed transfer is
external data: it must surface as controlled validation failure and the
bounded verification-only recovery, never HTTP 500 / stuck VERIFYING.
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
from trustflow.core.errors import PlanValidationError
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider, MetaResponsesProvider
from trustflow.llm.usage import LLMCallRecord
from trustflow.tools.ledger import strict_json_number

PASS = {"verdict": "PASS", "confidence": 0.8, "issues": [], "summary": "ok"}
HUGE = 10**400


def transfer(amount=73):
    return dict(tool_name="simulate_transfer", from_account="acc_alice",
                to_account="acc_bob", amount=amount, currency="USD")


class Sequence(FakeLLMProvider):
    def __init__(self, plans):
        super().__init__()
        self.plans, self.plan_calls = plans, 0

    def propose_plan(self, title, description, repair_feedback=None):
        index = min(self.plan_calls, len(self.plans) - 1)
        self.plan_calls += 1
        return copy.deepcopy(self.plans[index]), LLMCallRecord(
            model="fake", purpose="repair" if repair_feedback else "plan")


def box(**settings):
    c = Container(Settings(_env_file=None, llm_mode="fake", **settings))
    p = Sequence([[transfer()]])
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


def huge_confidence_meta(confidence):
    body = {"id": "huge-confidence",
            "usage": {"input_tokens": 7, "output_tokens": 11},
            "output_text": json.dumps({**PASS, "confidence": confidence})}
    return meta(lambda request: httpx.Response(200, json=body), llm_max_retries=0)


@pytest.mark.parametrize("confidence", [HUGE, -HUGE])
def test_n1_huge_confidence_recovers_after_commit(confidence):
    c, _ = box()
    c.engine.semantic._llm = huge_confidence_meta(confidence)
    with TestClient(create_app(c), raise_server_exceptions=False) as client:
        response = client.post("/workflows", json={"title": "payment"})
        assert response.status_code == 201
        run = c.repo.list()[0]
        assert run.status == RunStatus.VERIFICATION_PENDING
        assert amounts(c) == [73]

        base = f"/workflows/{run.run_id}"
        assert client.get(base).status_code == 200
        assert client.get(base + "/audit").status_code == 200
        assert client.post(base + "/reconcile").json()["status"] == (
            "VERIFICATION_PENDING")

        # Healthy reviewer: verification-only recovery completes once.
        c.engine.semantic._llm = FakeLLMProvider()
        retry = client.post(base + "/verify-retry").json()
        assert retry["status"] == "COMPLETED"
        assert retry["error"] is None
        assert amounts(c) == [73]

        events = client.get(base + "/audit").json()["events"]
        failures = [e for e in events if e["action"] == "semantic_review_failed"]
        assert [e["detail"]["attempt"] for e in failures] == [1]
        assert failures[0]["detail"]["recovery_state"] == "VERIFICATION_PENDING"

    # API-visible usage: the incurred semantic attempt is kept exactly
    # once, with its known token counts.
    semantic = [r for r in run.llm_calls if r["purpose"] == "semantic_review"]
    first = semantic[0]
    assert (first["input_tokens"], first["output_tokens"]) == (7, 11)
    assert [r["transport_attempt"] for r in semantic] == [1, 1][:len(semantic)]


@pytest.mark.parametrize("confidence", [HUGE, -HUGE])
def test_n1_repeated_huge_confidence_exhausts_bounded_attempts(confidence):
    c, _ = box(semantic_max_attempts=3)
    c.engine.semantic._llm = huge_confidence_meta(confidence)
    with TestClient(create_app(c), raise_server_exceptions=False) as client:
        response = client.post("/workflows", json={"title": "payment"})
        assert response.status_code == 201
        run = c.repo.list()[0]
        base = f"/workflows/{run.run_id}"
        final = None
        for _ in range(5):
            final = client.post(base + "/verify-retry").json()
        assert final["status"] == "FAILED"
        assert run.verification_attempts == 3
        assert amounts(c) == [73]
        events = client.get(base + "/audit").json()["events"]
        failures = [e for e in events if e["action"] == "semantic_review_failed"]
        assert [e["detail"]["attempt"] for e in failures] == [1, 2, 3]
        assert [e["detail"]["recovery_state"] for e in failures] == [
            "VERIFICATION_PENDING", "VERIFICATION_PENDING", "FAILED"]
    semantic = [r for r in run.llm_calls if r["purpose"] == "semantic_review"]
    assert len(semantic) == 3
    assert all((r["input_tokens"], r["output_tokens"]) == (7, 11)
               for r in semantic)
    assert [r["attempt"] for r in semantic] == [1, 2, 3]


@pytest.mark.parametrize("amount", [HUGE, -HUGE])
def test_n1_huge_amount_rejected_before_side_effects(amount):
    for step in (
        dict(tool_name="simulate_transfer", from_account="acc_alice",
             to_account="acc_bob", amount=amount, currency="USD"),
        dict(tool_name="fx_quote", from_currency="USD", to_currency="EUR",
             amount=amount),
    ):
        with pytest.raises(PlanValidationError):
            validate_steps([step])


def test_n1_strict_json_number_is_total_for_json_numbers():
    with pytest.raises(ValueError):
        strict_json_number(HUGE)
    with pytest.raises(ValueError):
        strict_json_number(-HUGE)
    for bad in (True, "0.8", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            strict_json_number(bad)
    assert strict_json_number(0) == 0.0
    assert strict_json_number(73) == 73.0


def test_n1_valid_confidence_and_amounts_still_accepted():
    for good in (0, 1, 0.8):
        parsed = SemanticReviewInput.model_validate({**PASS, "confidence": good})
        assert parsed.confidence == float(good)
    for bad in (True, "0.8"):
        with pytest.raises(ValidationError):
            SemanticReviewInput.model_validate({**PASS, "confidence": bad})
    step = validate_steps([dict(tool_name="simulate_transfer",
                                from_account="acc_alice", to_account="acc_bob",
                                amount=73.0, currency="USD")])
    assert step[0].args["amount"] == 73.0


async def test_n1_engine_level_no_verifying_deadlock():
    c, _ = box()
    c.engine.semantic._llm = huge_confidence_meta(HUGE)
    run = await c.engine.create_workflow(WorkflowGoal(title="payment"))
    assert run.status == RunStatus.VERIFICATION_PENDING
    assert amounts(c) == [73]
