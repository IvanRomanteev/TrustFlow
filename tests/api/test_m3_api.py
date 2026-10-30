"""M3 API: approval authorization, listing, restart durability, demo E2E.

Deterministic fake mode only. A "restart" is a brand-new Container
(new repositories, ledger, audit and engine) over the same DB file.
"""

import pytest
from fastapi.testclient import TestClient

from trustflow.api.app import Container, create_app
from trustflow.core.settings import Settings

TOKEN = "demo-approval-token"
HEADERS = {"X-Approval-Token": TOKEN}


def _container(tmp_path, **overrides) -> Container:
    settings = Settings(
        db_path=str(tmp_path / "demo.db"), approval_token=TOKEN, **overrides)
    return Container(settings)


def _client(container: Container) -> TestClient:
    return TestClient(create_app(container))


def _create_high_risk(client: TestClient) -> dict:
    resp = client.post("/workflows", json={
        "title": "Transfer 5000", "description": "transfer 5000 to bob"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "AWAITING_APPROVAL"
    return body


def test_health_reports_persistence_and_auth(tmp_path):
    resp = _client(_container(tmp_path)).get("/health")
    assert resp.status_code == 200
    assert resp.json()["persistence"] == "sqlite"
    assert resp.json()["approval_auth_required"] is True


def test_approval_requires_valid_token(tmp_path):
    client = _client(_container(tmp_path))
    body = _create_high_risk(client)
    url = f"/workflows/{body['run_id']}/approve"
    payload = {"actor": "mgr", "plan_hash": body["plan"]["plan_hash"]}
    assert client.post(url, json=payload).status_code == 401
    assert client.post(
        url, json=payload, headers={"X-Approval-Token": "wrong"}).status_code == 403
    # Failed auth attempts must not consume the single-use approval.
    ok = client.post(url, json=payload, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["status"] == "COMPLETED"


def test_workflow_list_and_get_after_restart(tmp_path):
    container = _container(tmp_path)
    body = _create_high_risk(_client(container))
    restarted = _client(_container(tmp_path))
    listed = restarted.get("/workflows")
    assert listed.status_code == 200
    assert [r["run_id"] for r in listed.json()] == [body["run_id"]]
    got = restarted.get(f"/workflows/{body['run_id']}")
    assert got.status_code == 200
    assert got.json()["status"] == "AWAITING_APPROVAL"
    assert got.json()["plan"]["plan_hash"] == body["plan"]["plan_hash"]


def test_approval_binding_survives_restart_single_transfer(tmp_path):
    container = _container(tmp_path)
    body = _create_high_risk(_client(container))
    restarted_container = _container(tmp_path)
    restarted = _client(restarted_container)
    url = f"/workflows/{body['run_id']}/approve"
    payload = {"actor": "mgr", "plan_hash": body["plan"]["plan_hash"]}
    ok = restarted.post(url, json=payload, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["status"] == "COMPLETED"
    assert restarted.post(url, json=payload, headers=HEADERS).status_code == 409
    txs = restarted_container.registry.ledger.transactions
    assert len([t for t in txs if t.amount == 5000.0]) == 1
    audit = restarted.get(f"/workflows/{body['run_id']}/audit")
    assert audit.status_code == 200
    assert any(e["action"] == "approval_granted" for e in audit.json()["events"])


def test_demo_ordinary_task_completes(tmp_path):
    client = _client(_container(tmp_path))
    resp = client.post("/workflows", json={
        "title": "Check balance", "description": "balance"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "COMPLETED"


def test_demo_semantic_failure_verification_only_recovery(tmp_path):
    # A semantic FAIL after a committed transfer must land in
    # VERIFICATION_PENDING; verify-retry re-checks without a 2nd transfer.
    from trustflow.llm.provider import FakeLLMProvider

    container = _container(tmp_path)
    failing = FakeLLMProvider(review={
        "verdict": "FAIL", "confidence": 0.9,
        "issues": ["injected semantic failure"], "summary": "injected"})
    container.engine.semantic._llm = failing
    client = _client(container)
    body = _create_high_risk(client)
    url = f"/workflows/{body['run_id']}/approve"
    payload = {"actor": "mgr", "plan_hash": body["plan"]["plan_hash"]}
    pending = client.post(url, json=payload, headers=HEADERS)
    assert pending.status_code == 200
    assert pending.json()["status"] == "VERIFICATION_PENDING"
    txs = [t for t in container.registry.ledger.transactions if t.amount == 5000.0]
    assert len(txs) == 1
    # Healthy reviewer: verification-only retry completes, still one tx.
    container.engine.semantic._llm = FakeLLMProvider()
    retried = client.post(
        f"/workflows/{body['run_id']}/verify-retry", headers=HEADERS)
    assert retried.status_code == 200
    assert retried.json()["status"] == "COMPLETED"
    txs = [t for t in container.registry.ledger.transactions if t.amount == 5000.0]
    assert len(txs) == 1


def test_demo_mandatory_evidence_failure_blocks_before_mutation(tmp_path):
    # require_evidence with no retrievable policy evidence fails
    # before planning/execution: FAILED and zero ledger transactions.
    container = _container(tmp_path)
    client = _client(container)
    resp = client.post("/workflows", json={
        "title": "zzqqxxyy wwkknn", "description": "zzqq wwkknn",
        "require_evidence": True})
    assert resp.status_code == 201
    assert resp.json()["status"] == "FAILED"
    assert container.registry.ledger.transactions == []
    # A fabricated approval hash is likewise rejected without mutation.
    body = _create_high_risk(client)
    bad = client.post(
        f"/workflows/{body['run_id']}/approve",
        json={"actor": "mgr", "plan_hash": "0" * 64}, headers=HEADERS)
    assert bad.status_code == 409
    assert container.registry.ledger.transactions == []


@pytest.mark.parametrize("endpoint", ["verify-retry", "reconcile"])
def test_recovery_endpoints_require_token(tmp_path, endpoint):
    client = _client(_container(tmp_path))
    body = _create_high_risk(client)
    url = f"/workflows/{body['run_id']}/{endpoint}"
    assert client.post(url).status_code == 401
    assert client.post(url, headers={"X-Approval-Token": "no"}).status_code == 403
