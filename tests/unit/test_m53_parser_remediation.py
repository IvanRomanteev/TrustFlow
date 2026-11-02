"""M5.3 F1-A..I and H1 regressions: local MockTransport and synthetic SQLite only."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from trustflow.api.factory import Container, create_app
from trustflow.core.errors import (
    LiveBudgetExceededError,
    LLMResponseError,
    TokenBudgetExceededError,
)
from trustflow.evaluation.live import EvaluationSettings
from trustflow.llm.provider import REVIEW_SCHEMA, MetaResponsesProvider
from trustflow.llm.usage import UsageTracker, estimate_cost

MODEL = "muse-spark-1.3"
GOOD = {"verdict": "PASS", "confidence": 1, "issues": [], "summary": "Evidence agrees"}
DEPTH = 5000
PRIVATE = "private-output-must-not-be-published"
NESTED = ('{"summary":"' + PRIVATE + '","issues":' + "[" * DEPTH
          + "0" + "]" * DEPTH + '}')
PAYMENT = {"tool_name": "simulate_transfer", "account_id": "",
           "from_account": "acc_alice", "to_account": "acc_bob", "amount": 1500,
           "currency": "USD", "from_currency": "", "to_currency": "", "limit": 1,
           "rationale": "Synthetic payment"}


@pytest.fixture(autouse=True)
def mock_only(monkeypatch):
    # Also applies when this exact file is run externally for RED, without conftest.py.
    monkeypatch.setenv("MODEL_API_KEY", "offline-remediation-placeholder")

    def forbidden(*args, **kwargs):
        raise AssertionError("F1 regressions may only use MockTransport/ASGITransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def envelope(text, model=MODEL, usage=None):
    return {"id": "resp_f1", "model": model, "status": "completed",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text}]}],
            "usage": usage if usage is not None else {"input_tokens": 111, "output_tokens": 37}}


def healthy(model=MODEL, payload=None):
    return httpx.Response(200, json=envelope(json.dumps(payload or GOOD), model))


def malformed(layer, model=MODEL, usage=None):
    if layer == "inner":
        response = httpx.Response(200, json=envelope(NESTED, model, usage))
    else:
        # Usage text exists in the raw body but cannot be treated as authoritative
        # before parsing the entire envelope. Never scrape it from invalid JSON.
        response = httpx.Response(200, content=(
            '{"usage":{"input_tokens":111,"output_tokens":37},"private":"'
            + PRIVATE + '","payload":' + "[" * DEPTH + "0" + "]" * DEPTH + "}"
        ).encode())
    return response


class Responses:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        assert request.url == "https://api.meta.ai/v1/responses"
        assert request.headers["authorization"] == "Bearer offline-transport-placeholder"
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0)
        response.headers["x-request-id"] = f"req_f1_{len(self.requests)}"
        return response


def provider(responses, **options):
    settings = {"llm_mode": "live", "planning_model": MODEL, "review_model": MODEL,
                "llm_max_retries": 0, "llm_backoff_seconds": 0,
                "max_output_tokens": 16384, "llm_max_cost_usd": 1}
    return MetaResponsesProvider(EvaluationSettings(**(settings | options)),
                                 transport=httpx.MockTransport(responses))


def call(p, tracker=None):
    return p._call("semantic_review", REVIEW_SCHEMA, "Synthetic fixture", "semantic_review",
                   tracker=tracker, retry_reserve_tokens=17000)


def collect_failure(p, tracker):
    with pytest.raises(LLMResponseError) as error:
        call(p, tracker)
    exc = error.value
    assert PRIVATE not in str(exc) and exc.__cause__ is None
    if tracker is None:
        assert exc.record is exc.records[-1]
        tracker = UsageTracker(100000, "outside-caller")
        tracker.record(exc.record)
    else:
        # A tracker-aware provider owns failed records. Callers must not receive
        # them a second time through LLMResponseError.record/prior_attempts.
        assert exc.record is None and exc.records == []
    return tracker


@pytest.mark.parametrize("model", [MODEL, "muse-wonder-1.0"])
@pytest.mark.parametrize("layer", ["inner", "outer"])
@pytest.mark.parametrize("tracked", [False, True])
def test_f1_a_to_d_parser_failure_has_one_owned_record(layer, tracked, model):
    responses = Responses(malformed(layer, model))
    p = provider(responses, planning_model=model, review_model=model)
    tracker = UsageTracker(100000, "originating-workflow") if tracked else None
    records = collect_failure(p, tracker).records
    assert len(responses.requests) == p.budget.requests == len(records) == 1
    record = records[0]
    assert record.model == model and record.purpose == "semantic_review"
    assert record.transport_attempt == record.attempt == 1
    assert record.request_id == "req_f1_1" and record.http_status == 200
    assert record.validation_outcome == "failed" and record.failure_reason
    assert record.prior_attempts == []
    if tracked:
        assert record.workflow_id == "originating-workflow"
    if layer == "inner":
        assert (record.input_tokens, record.output_tokens) == (111, 37)
        assert not record.usage_unknown
        assert record.budget_charge_usd == estimate_cost(model, 111, 37)
    else:
        assert record.input_tokens is record.output_tokens is None
        assert record.usage_unknown and record.budget_charge_usd > 0
        assert record.response_id is None
    assert p.budget.charged_usd == pytest.approx(record.budget_charge_usd)


@pytest.mark.parametrize("layer", ["inner", "outer"])
@pytest.mark.parametrize("tracked", [False, True])
def test_f1_e_success_before_and_after_failure_is_not_double_counted(layer, tracked):
    responses = Responses(healthy(), malformed(layer), healthy())
    p = provider(responses)
    owner = UsageTracker(100000, "one-workflow")
    _, record = call(p, owner if tracked else None)
    owner.record(record)
    with pytest.raises(LLMResponseError) as error:
        call(p, owner if tracked else None)
    if tracked:
        assert error.value.record is None
    else:
        owner.record(error.value.record)
    _, record = call(p, owner if tracked else None)
    owner.record(record)
    assert len(owner.records) == p.budget.requests == 3
    assert [r.attempt for r in owner.records] == [1, 2, 3]
    assert [r.request_id for r in owner.records] == ["req_f1_1", "req_f1_2", "req_f1_3"]
    assert [r.validation_outcome for r in owner.records] == [
        "json_object_valid", "failed", "json_object_valid"]
    assert owner.total_tokens == (444 if layer == "inner" else 296)


@pytest.mark.parametrize("layer", ["inner", "outer"])
@pytest.mark.parametrize("tracked", [False, True])
def test_f1_f_retry_then_malformed_terminal_preserves_attempt_ownership(layer, tracked):
    responses = Responses(httpx.Response(503, json={
        "usage": {"input_tokens": 5, "output_tokens": 9}}), malformed(layer))
    p = provider(responses, llm_max_retries=2)
    tracker = UsageTracker(100000, "retry-workflow") if tracked else None
    owner = collect_failure(p, tracker)
    assert len(owner.records) == p.budget.requests == 2  # malformed HTTP 200 is terminal
    assert [r.transport_attempt for r in owner.records] == [1, 2]
    assert [r.attempt for r in owner.records] == [1, 1]
    assert [r.http_status for r in owner.records] == [503, 200]
    assert all(r.prior_attempts == [] for r in owner.records)
    assert owner.total_tokens == (162 if layer == "inner" else 14)
    assert p.budget.charged_usd == pytest.approx(
        sum(r.budget_charge_usd for r in owner.records))


@pytest.mark.parametrize("limit", ["requests", "cost"])
@pytest.mark.parametrize("tracked", [False, True])
def test_f1_outer_unknown_usage_keeps_request_and_cost_guards(limit, tracked):
    responses = Responses(malformed("outer"))
    options = {"llm_max_requests": 1} if limit == "requests" else {"llm_max_cost_usd": 0.1}
    p = provider(responses, **options)
    tracker = UsageTracker(100000, "limited") if tracked else None
    owner = collect_failure(p, tracker)
    charged = p.budget.charged_usd
    with pytest.raises(LiveBudgetExceededError):
        call(p, tracker)
    assert p.budget.requests == len(responses.requests) == 1
    assert len(owner.records) == 1 and p.budget.charged_usd == charged > 0


def sqlite_state(db):
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        return {"transactions": conn.execute(
            "SELECT tx_id,idempotency_key,from_account,to_account,amount,currency "
            "FROM ledger_transactions ORDER BY seq").fetchall(),
            "accounts": conn.execute(
                "SELECT account_id,balance FROM ledger_accounts ORDER BY account_id").fetchall(),
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0]}
    finally:
        conn.close()


def durable_run(db, run_id):
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return json.loads(conn.execute(
            "SELECT run_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def boxes():
    opened = []
    yield opened
    for box in opened:
        for resource in (box.registry.ledger, box.repo, box.audit):
            resource._conn.close()


def api(box):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(box), raise_app_exceptions=False),
        base_url="http://synthetic.local")


HEADERS = {"X-Approval-Token": "synthetic-f1-approval"}


async def approved_failure(box, db):
    async with api(box) as client:
        created = await client.post("/workflows", json={"title": "Transfer 1500 USD"})
        assert created.status_code == 201
        pending = created.json()
        assert pending["status"] == "AWAITING_APPROVAL"
        path = "/workflows/" + pending["run_id"]
        approved = await client.post(path + "/approve", headers=HEADERS,
                                     json={"actor": "operator",
                                           "plan_hash": pending["plan"]["plan_hash"]})
        assert approved.status_code == 200
        failed = approved.json()
        assert failed["status"] == "VERIFICATION_PENDING"
        assert failed["verification_attempts"] == 1
        stored = durable_run(db, failed["run_id"])
        assert stored["status"] == failed["status"]
        assert len(stored["llm_calls"]) == len(failed["llm_calls"]) == 2
        assert PRIVATE not in json.dumps(failed)
        return path, failed


def settings(db, **options):
    return EvaluationSettings(llm_mode="live", planning_model=MODEL, review_model=MODEL,
                              db_path=str(db), approval_token=HEADERS["X-Approval-Token"],
                              max_output_tokens=16384, token_budget_per_workflow=100000,
                              semantic_max_attempts=2, max_retries=0, **options)


@pytest.mark.parametrize("layer", ["inner", "outer"])
@pytest.mark.parametrize("restart", [False, True])
async def test_f1_g_h_i_committed_payment_recovers_with_complete_durable_usage(
        tmp_path, boxes, monkeypatch, layer, restart):
    db = tmp_path / "synthetic.db"
    responses = Responses(healthy(payload={"steps": [PAYMENT]}), malformed(layer), healthy())
    p = provider(responses)
    box = Container(settings(db), provider=p)
    boxes.append(box)
    path, failed = await approved_failure(box, db)
    before = sqlite_state(db)
    assert before["integrity"] == "ok" and len(before["transactions"]) == 1
    assert before["transactions"][0][4] == 1500
    assert before["transactions"][0][1] == failed["plan"]["steps"][0]["operation_id"]
    assert failed["plan"]["plan_hash"] == failed["approved_hash"] == (
        failed["approved_plan_snapshot"]["plan_hash"])
    if restart:
        # A new provider/container has a new session budget; durable workflow usage
        # and semantic attempts must survive independently of that session state.
        p = provider(responses)
        box = Container(settings(db), provider=p)
        boxes.append(box)
        assert len(durable_run(db, failed["run_id"])["llm_calls"]) == 2
        assert len(responses.requests) == 2  # startup does not call a model

    def no_tools(*args, **kwargs):
        pytest.fail("verification-only retry invoked a tool")

    monkeypatch.setattr(box.registry, "invoke", no_tools)
    async with api(box) as client:
        response = await client.post(path + "/verify-retry", headers=HEADERS)
        assert response.status_code == 200
        final = response.json()
        assert final["status"] == "COMPLETED" and final["verification_attempts"] == 2
        assert final["approved_hash"] == failed["approved_hash"]
        assert final["approved_plan_snapshot"] == failed["approved_plan_snapshot"]
        assert sqlite_state(db) == before
        calls = durable_run(db, final["run_id"])["llm_calls"]
        assert len(calls) == len(responses.requests) == 3
        assert [r["purpose"] for r in calls] == ["plan", "semantic_review", "semantic_review"]
        assert [r["attempt"] for r in calls] == [1, 1, 2]
        assert [r["request_id"] for r in calls] == ["req_f1_1", "req_f1_2", "req_f1_3"]
        assert all(r["workflow_id"] == final["run_id"] for r in calls)
        assert calls[1]["validation_outcome"] == "failed"
        assert calls[1]["usage_unknown"] == (layer == "outer")
        assert calls[1]["input_tokens"] == (111 if layer == "inner" else None)
        assert calls[1]["output_tokens"] == (37 if layer == "inner" else None)
        assert calls[1]["budget_charge_usd"] > 0
        assert (await client.post(path + "/verify-retry", headers=HEADERS)).json() == final
        assert len(responses.requests) == 3


@pytest.mark.parametrize("layer", ["inner", "outer"])
async def test_f1_semantic_attempt_exhaustion_remains_durable(tmp_path, boxes, layer):
    db = tmp_path / "synthetic.db"
    responses = Responses(healthy(payload={"steps": [PAYMENT]}),
                          malformed(layer), malformed(layer))
    box = Container(settings(db), provider=provider(responses))
    boxes.append(box)
    path, initial = await approved_failure(box, db)
    before = sqlite_state(db)
    async with api(box) as client:
        final = (await client.post(path + "/verify-retry", headers=HEADERS)).json()
        assert final["status"] == "FAILED" and final["verification_attempts"] == 2
        again = (await client.post(path + "/verify-retry", headers=HEADERS)).json()
        assert again["status"] == "FAILED" and len(responses.requests) == 3
    assert sqlite_state(db) == before
    assert len(durable_run(db, initial["run_id"])["llm_calls"]) == 3


async def test_f1_reported_usage_overshoot_blocks_recovery_http_even_after_restart(
        tmp_path, boxes):
    db = tmp_path / "synthetic.db"
    responses = Responses(healthy(payload={"steps": [PAYMENT]}), malformed(
        "inner", usage={"input_tokens": 100001, "output_tokens": 37}))
    box = Container(settings(db), provider=provider(responses))
    boxes.append(box)
    path, failed = await approved_failure(box, db)
    assert failed["llm_calls"][1]["over_budget"]
    assert failed["llm_calls"][1]["input_tokens"] == 100001
    before = sqlite_state(db)
    reopened = Container(settings(db), provider=provider(responses))
    boxes.append(reopened)
    async with api(reopened) as client:
        final = (await client.post(path + "/verify-retry", headers=HEADERS)).json()
        assert final["status"] == "FAILED"
    assert len(responses.requests) == len(final["llm_calls"]) == 2
    assert sqlite_state(db) == before


def test_f1_known_usage_above_token_budget_is_kept_once():
    responses = Responses(malformed("inner"))
    p = provider(responses)
    tracker = UsageTracker(100, "over-budget")
    with pytest.raises(TokenBudgetExceededError):
        call(p, tracker)
    assert p.budget.requests == len(tracker.records) == 1
    assert tracker.total_tokens == 148 and tracker.records[0].over_budget


NOW = datetime(2026, 11, 3, tzinfo=UTC)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@pytest.mark.parametrize("header,wait", [
    ("2", 2), ("60", None),
    (format_datetime(NOW + timedelta(seconds=2), usegmt=True), 2),
    (format_datetime(NOW + timedelta(seconds=60), usegmt=True), None),
    (format_datetime(NOW - timedelta(seconds=10), usegmt=True), 0.5),
    (format_datetime(NOW, usegmt=True), 0.5),
    ("invalid-date", 0.5), ("Tue, 03 Nov 2026 00:00:02", 0.5),
    ("Tue, 03 Nov 999999 00:00:00 GMT", 0.5), ("nan", 0.5), ("inf", 0.5),
])
def test_h1_retry_after_date_and_numeric_are_bounded(monkeypatch, header, wait):
    # raising=False lets the same regression file execute on exact ef30a688,
    # where the old implementation has no datetime dependency.
    monkeypatch.setattr("trustflow.llm.provider.datetime", FrozenDateTime, raising=False)
    sleeps = []
    monkeypatch.setattr("trustflow.llm.provider.time.sleep", sleeps.append)
    responses = Responses(httpx.Response(429, headers={"Retry-After": header}), healthy())
    p = provider(responses, llm_max_retries=1, llm_backoff_seconds=0.5)
    if wait is None:
        with pytest.raises(LLMResponseError):
            call(p)
        assert len(responses.requests) == p.budget.requests == 1 and sleeps == []
    else:
        result, record = call(p)
        assert result == GOOD and len(record.prior_attempts) == 1
        assert len(responses.requests) == p.budget.requests == 2
        assert len(sleeps) == 1 and wait <= sleeps[0] <= wait + 0.05
