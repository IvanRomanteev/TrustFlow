"""Actual reviewer/engine contracts with a data-dependent, synthetic oracle.

The MockTransport oracle checks evidence relationships; it does not predict Muse
quality or prove that a future real model will agree. Negative controls must
remain FAIL even when the execution itself committed successfully.
"""

import json
import re
from pathlib import Path

import httpx
import pytest

from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.semantic_evidence import review_context_kwargs
from trustflow.agents.verifier import Verifier
from trustflow.api.factory import Container, create_app
from trustflow.domain.models import WorkflowGoal
from trustflow.evaluation.live import EvaluationSettings, InvalidFirstReview, sql_evidence
from trustflow.llm.provider import PLAN_SCHEMA, REVIEW_SCHEMA, MetaResponsesProvider
from trustflow.llm.usage import UsageTracker
from trustflow.tools.ledger import SyntheticLedger

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/m53_recovery_semantic.json").read_text(
        encoding="utf-8"
    )
)
HEADERS = {"X-Approval-Token": "semantic-evidence-test"}


def rational_verdict(instructions, payload):
    """Evaluate linked evidence and goal, never select a verdict by test name."""
    e = payload.get("execution_evidence") or {}
    issues = []
    if "atomically commits" not in instructions:
        issues.append("Synthetic mutation semantics unavailable")
    if e.get("approval_binding_matches") is not True or e.get("human_approval_matches") is not True:
        issues.append("Approval binding unavailable or mismatched")
    if not (e.get("deterministic_verification") or {}).get("passed"):
        issues.append("Execution verification not passed")
    amount = re.search(r"\d+", payload["goal"]["title"])
    ops = e.get("operations") or []
    if not ops:
        issues.append("No independently linked ledger operation")
    for op in ops:
        tx = op.get("ledger_transaction")
        if not tx or not op["matches_plan"] or not op["matches_worker"]:
            issues.append("Missing or mismatched ledger/worker evidence")
            continue
        if not op["intent_authorized"] or not op["authorized_payload_matches"]:
            issues.append("Intent was not authorized for the payload")
        if tx["idempotency_key"] != op["operation_id"] or op["intent_id"] != op["operation_id"]:
            issues.append("Operation identity mismatch")
        if amount and tx["amount"] != float(amount[0]):
            issues.append("Goal amount differs from committed amount")
    return {
        "verdict": "FAIL" if issues else "PASS",
        "confidence": 0.95,
        "issues": issues,
        "summary": "Deterministic synthetic evidence-contract oracle",
    }


class Responses:
    def __init__(self, *, force_disagreement=False):
        self.bodies = []
        self.payloads = []
        self.force_disagreement = force_disagreement

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        assert body["text"]["format"]["strict"] is True
        if body["text"]["format"]["name"] == "plan_steps":
            assert body["text"]["format"]["schema"] == PLAN_SCHEMA
            defaults = {
                "account_id": "",
                "from_account": "",
                "to_account": "",
                "amount": 0,
                "currency": "",
                "from_currency": "",
                "to_currency": "",
                "limit": 1,
                "rationale": "synthetic test fixture",
            }
            value = {"steps": [defaults | s for s in FIXTURE["steps"]]}
        else:
            assert body["text"]["format"]["schema"] == REVIEW_SCHEMA
            instructions, raw = body["input"].split("\n", 1)
            payload = json.loads(raw)
            self.payloads.append(payload)
            value = (
                FIXTURE["observed_semantic_rejection"]
                if self.force_disagreement
                else rational_verdict(instructions, payload)
            )
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "status": "completed",
                "output": [
                    {"type": "reasoning", "status": "completed"},
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(value)}],
                    },
                ],
                "usage": {
                    "input_tokens": 211,
                    "output_tokens": 144,
                    "output_tokens_details": {"reasoning_tokens": 100},
                },
            },
        )


def settings(db, attempts=2):
    return EvaluationSettings(
        llm_mode="live",
        db_path=str(db),
        approval_token=HEADERS["X-Approval-Token"],
        planning_model="muse-spark-1.3",
        review_model="muse-spark-1.3",
        max_output_tokens=4096,
        llm_reasoning_effort="low",
        llm_max_retries=0,
        max_retries=0,
        token_budget_per_workflow=30000,
        semantic_max_attempts=attempts,
    )


def provider(responses):
    return MetaResponsesProvider(settings("unused"), transport=httpx.MockTransport(responses))


@pytest.fixture
def boxes():
    opened = []
    yield opened
    for box in opened:
        for resource in [box.registry.ledger, box.repo, box.audit]:
            resource._conn.close()


def client(box):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(box)), base_url="http://test"
    )


async def create_approve(box, *, prior=True):
    if prior:
        box.registry.ledger.transfer("acc_alice", "acc_bob", 1500, "unrelated-prior-operation")
    async with client(box) as api:
        created = (await api.post("/workflows", json=FIXTURE["goal"])).json()
        assert created["status"] == "AWAITING_APPROVAL"
        path = "/workflows/" + created["run_id"]
        wrong = await api.post(
            path + "/approve", headers=HEADERS, json={"actor": "operator", "plan_hash": "0" * 64}
        )
        assert wrong.status_code == 409
        result = await api.post(
            path + "/approve",
            headers=HEADERS,
            json={"actor": "operator", "plan_hash": created["plan"]["plan_hash"]},
        )
        assert result.status_code == 200
        return path, result.json()


@pytest.mark.parametrize("restart", [False, True])
async def test_valid_committed_recovery_supplies_binding_and_never_reexecutes(
    tmp_path, boxes, monkeypatch, restart
):
    db = tmp_path / "owned.db"
    responses = Responses()
    p = provider(responses)
    box = Container(settings(db), provider=p)
    boxes.append(box)
    box.engine.semantic._llm = InvalidFirstReview(p)
    path, pending = await create_approve(box)
    assert pending["status"] == "VERIFICATION_PENDING"
    before = sql_evidence(db)
    assert len(before["transactions"]) == 2
    if restart:
        box = Container(settings(db), provider=p)
        boxes.append(box)

    def forbidden(*args, **kwargs):
        pytest.fail("Verification-only retry executed a tool")

    monkeypatch.setattr(box.registry, "invoke", forbidden)
    async with client(box) as api:
        final = (await api.post(path + "/verify-retry", headers=HEADERS)).json()
        assert final["status"] == "COMPLETED" and final["verification_attempts"] == 2
        assert (await api.post(path + "/verify-retry", headers=HEADERS)).json() == final
    assert sql_evidence(db) == before
    assert final["approved_hash"] == pending["approved_hash"]
    assert final["approved_plan_snapshot"] == pending["approved_plan_snapshot"]
    assert len(responses.bodies) == len(final["llm_calls"]) == 3
    e = responses.payloads[-1]["execution_evidence"]
    assert (
        e["approved_hash"]
        == e["approved_snapshot_hash"]
        == e["plan_hash"]
        == final["approved_hash"]
    )
    assert e["verification_only_retry"] is True
    assert e["deterministic_verification"]["source"] == "Verifier.verify"
    op = e["operations"][0]
    assert op["matches_plan"] and op["matches_worker"]
    assert op["ledger_transaction"]["tx_id"] == "tx_0002"
    assert op["ledger_transaction"]["idempotency_key"] == op["operation_id"]
    assert op["authorized_payload_matches"] and op["intent_authorized"]
    assert responses.payloads[0]["execution_evidence"]["verification_only_retry"] is False


@pytest.mark.parametrize(
    "fault", ["goal_amount", "operation_id", "tx_id", "missing_ledger", "approval_hash"]
)
async def test_negative_evidence_controls_remain_semantic_fail(tmp_path, boxes, fault):
    responses = Responses()
    p = provider(responses)
    box = Container(settings(tmp_path / "owned.db"), provider=p)
    boxes.append(box)
    _, final = await create_approve(box)
    assert final["status"] == "COMPLETED"
    run = box.repo.get(final["run_id"])
    run = run.model_copy(deep=True)
    plan = run.approved_plan_snapshot
    goal = run.goal
    ledger = box.registry.ledger
    if fault == "goal_amount":
        goal = WorkflowGoal(title="Transfer 1300 USD", description="Send exactly 1300 USD")
    if fault == "operation_id":
        plan.steps[-1].operation_id = "wrong-operation"
    if fault == "tx_id":
        run.step_results[-1].data["tx_id"] = "wrong-tx"
    if fault == "missing_ledger":
        ledger = SyntheticLedger()
        # Preserve the independently returned historical read; remove only
        # the recovery operation's commit. A wholly absent read ledger is
        # already rejected by the reviewer's pre-inference evidence guard.
        ledger.transfer("acc_alice", "acc_bob", 1500, "unrelated-prior-operation")
    if fault == "approval_hash":
        run.approved_hash = "f" * 64
    deterministic = Verifier(ledger, 16200).verify(run, plan)
    reviewer = SemanticReviewer(p, ledger, max_output_tokens=4096)
    outcome = reviewer.review(
        goal,
        plan,
        tracker=UsageTracker(30000, "negative-" + fault),
        step_results=run.step_results,
        **review_context_kwargs(reviewer, run, deterministic, True),
    )
    assert outcome.verdict == "FAIL"
    payload = responses.payloads[-1]
    if fault == "operation_id":
        assert payload["execution_evidence"]["operations"][0]["ledger_transaction"] is None
    if fault == "tx_id":
        assert payload["execution_evidence"]["operations"][0]["matches_worker"] is False
    if fault == "approval_hash":
        assert payload["execution_evidence"]["approval_binding_matches"] is False


async def test_worker_only_simulation_without_commit_is_not_ledger_evidence(
    tmp_path, boxes, monkeypatch
):
    responses = Responses()
    p = provider(responses)
    box = Container(settings(tmp_path / "owned.db"), provider=p)
    boxes.append(box)
    original = box.registry.invoke

    def dry_run(name, args):
        if name == "simulate_transfer":
            return {"tx_id": "worker-only-claim", **args}
        return original(name, args)

    monkeypatch.setattr(box.registry, "invoke", dry_run)
    _, final = await create_approve(box, prior=False)
    assert final["status"] == "FAILED"
    assert not sql_evidence(tmp_path / "owned.db")["transactions"]
    assert not any(
        c["name"].startswith("transfer_ledger_match:") and c["passed"]
        for c in final["verification"]["checks"]
    )
    assert responses.payloads == []  # Deterministic failure cannot be rescued by model PASS.


async def test_real_commit_then_semantic_disagreement_remains_truthful_failure(tmp_path, boxes):
    responses = Responses(force_disagreement=True)
    p = provider(responses)
    db = tmp_path / "owned.db"
    box = Container(settings(db, attempts=1), provider=p)
    boxes.append(box)
    path, final = await create_approve(box)
    assert final["status"] == "FAILED" and final["verification"]["semantic"]["verdict"] == "FAIL"
    assert all(c["passed"] for c in final["verification"]["checks"])
    assert "not rolled back" in final["error"]
    assert final["step_results"][-1]["data"]["tx_id"] == "tx_0002"
    before = sql_evidence(db)
    async with client(box) as api:
        audit = (await api.get(path + "/audit")).json()["events"]
        await api.post(path + "/verify-retry", headers=HEADERS)
    event = next(e for e in audit if e["action"] == "verification_finished")
    assert event["detail"]["deterministic_passed"] is True
    assert event["detail"]["semantic_verdict"] == "FAIL" and event["detail"]["passed"] is False
    assert sql_evidence(db) == before and len(responses.bodies) == 2


def test_legacy_payload_cannot_prove_operation_attribution_to_contract_oracle():
    payload = {
        "goal": FIXTURE["goal"],
        "plan_steps": FIXTURE["steps"],
        "evidence": {
            "transactions": [
                {"tx_id": "tx_0002", "amount": 1200, "idempotency_key": "unlinked-operation"}
            ]
        },
    }
    assert rational_verdict("Review synthetic workflow", payload)["verdict"] == "FAIL"
