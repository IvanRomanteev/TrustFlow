"""M5.1 deployment contract: CORS, fail-closed LLM config, build context.

Deterministic fake mode only; no network, no paid calls. These tests pin
the deployment-facing behaviour validated live against Docker Compose
(see docs/m5.md): browser-origin handling for the demo UI, startup that
fails closed on bad LLM configuration instead of silently falling back,
and Docker build contexts that cannot leak host build artifacts.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trustflow.api.app import Container, create_app
from trustflow.core.errors import LLMNotConfiguredError
from trustflow.core.settings import Settings
from trustflow.llm.provider import build_provider

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = "http://127.0.0.1:3000"


def _client(tmp_path) -> TestClient:
    settings = Settings(
        db_path=str(tmp_path / "demo.db"), approval_token="demo-approval-token")
    return TestClient(create_app(Container(settings)))


def test_cors_allows_demo_origin_only(tmp_path):
    client = _client(tmp_path)
    ok = client.get("/health", headers={"Origin": ALLOWED})
    assert ok.headers.get("access-control-allow-origin") == ALLOWED
    evil = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_cors_preflight_permits_approval_token_header(tmp_path):
    client = _client(tmp_path)
    resp = client.options("/workflows", headers={
        "Origin": ALLOWED,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-approval-token",
    })
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED
    assert "x-approval-token" in resp.headers.get(
        "access-control-allow-headers", "").lower()


def test_unsupported_llm_mode_fails_closed():
    with pytest.raises(ValueError, match="TRUSTFLOW_LLM_MODE"):
        Settings(llm_mode="bogus")


def test_live_mode_without_key_fails_closed(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        build_provider(Settings(llm_mode="live", llm_api_key=None))


def test_dockerignore_keeps_host_artifacts_out_of_images():
    frontend = (ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8")
    assert "node_modules" in frontend
    assert ".next" in frontend
    backend = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".venv" in backend
    assert "data" in backend
