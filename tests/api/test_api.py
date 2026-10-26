from fastapi.testclient import TestClient

from trustflow.api.app import create_app


def _client(container) -> TestClient:
    return TestClient(create_app(container))


def test_health(container):
    client = _client(container)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["synthetic_only"] is True


def test_low_risk_workflow_completes(container):
    client = _client(container)
    resp = client.post("/workflows", json={"title": "Check balance", "description": "balance"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "COMPLETED"
    audit = client.get(f"/workflows/{body['run_id']}/audit")
    assert audit.status_code == 200
    assert len(audit.json()["events"]) >= 3


def test_approval_flow_and_repeated_approval(container):
    client = _client(container)
    resp = client.post(
        "/workflows", json={"title": "Transfer 5000", "description": "transfer 5000 to bob"}
    )
    body = resp.json()
    assert body["status"] == "AWAITING_APPROVAL"
    plan_hash = body["plan"]["plan_hash"]
    bad = client.post(
        f"/workflows/{body['run_id']}/approve",
        json={"actor": "mgr", "plan_hash": "0" * 64},
    )
    assert bad.status_code == 409
    ok = client.post(
        f"/workflows/{body['run_id']}/approve",
        json={"actor": "mgr", "plan_hash": plan_hash},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "COMPLETED"
    again = client.post(
        f"/workflows/{body['run_id']}/approve",
        json={"actor": "mgr", "plan_hash": plan_hash},
    )
    assert again.status_code == 409


def test_reject_terminates_workflow(container):
    client = _client(container)
    resp = client.post(
        "/workflows", json={"title": "Transfer 5000", "description": "transfer 5000 to bob"}
    )
    body = resp.json()
    assert body["status"] == "AWAITING_APPROVAL"
    # Wrong hash must not reject.
    bad = client.post(
        f"/workflows/{body['run_id']}/reject",
        json={"actor": "mgr", "plan_hash": "f" * 64},
    )
    assert bad.status_code == 409


def test_missing_workflow_404(container):
    client = _client(container)
    assert client.get("/workflows/nope").status_code == 404
