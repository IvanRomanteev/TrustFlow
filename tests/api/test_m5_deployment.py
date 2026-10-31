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


# --- Effective .dockerignore semantics (P2-02/P3-04) -----------------
# Substring presence proves nothing (comments-only files passed the old
# test). These tests evaluate rule *effect* with Docker's documented
# semantics: `#` lines are comments, `!` negates, the last matching
# rule decides, an excluded directory excludes its subtree, `*` stays
# within one path segment and `**` crosses segments. The real Docker
# matcher is verified separately by the opt-in integration gate in
# tests/integration/test_m5_docker_e2e.py.

from fnmatch import fnmatchcase  # noqa: E402


def _seg_match(pattern: list[str], segments: list[str]) -> bool:
    if not pattern:
        return not segments
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return _seg_match(rest, segments) or (
            bool(segments) and _seg_match(pattern, segments[1:]))
    if not segments:
        return False
    return fnmatchcase(segments[0], head) and _seg_match(rest, segments[1:])


def _pattern_matches(pattern: str, path: str) -> bool:
    pat = [s for s in pattern.strip().strip("/").split("/") if s]
    segs = [s for s in path.split("/") if s]
    # A matching ancestor directory excludes everything below it.
    return any(_seg_match(pat, segs[:i]) for i in range(1, len(segs) + 1))


def _ignored(rules_text: str, path: str) -> bool:
    ignored = False
    for raw in rules_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if _pattern_matches(line[1:] if negated else line, path):
            ignored = not negated
    return ignored


def test_dockerignore_frontend_effective_rules():
    rules = (ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8")
    for artifact in ("node_modules/next/dist/bin/next", ".next/BUILD_ID",
                     "packages/p/node_modules/NESTED_NODE",
                     "packages/p/.next/NESTED_NEXT",
                     "packages/p/npm-debug.log.1", "npm-debug.log.2"):
        assert _ignored(rules, artifact), artifact
    for required in ("package.json", "package-lock.json", "Dockerfile",
                     "app/page.tsx", "lib/api.ts", "packages/p/package.json"):
        assert not _ignored(rules, required), required


def test_dockerignore_comments_or_negation_keep_artifacts():
    # P2-02 RED condition: pattern names in comments exclude nothing,
    # and a trailing negation cancels an exclusion. Both must read as
    # "not ignored", so coverage like the old substring test cannot
    # mistake them for working rules.
    comments_only = "# node_modules\n# .next\n# **/node_modules\n"
    assert not _ignored(comments_only, "node_modules/next/dist/bin/next")
    assert not _ignored(comments_only, "packages/p/.next/NESTED_NEXT")
    negated = "**/node_modules\n!packages/p/node_modules\n"
    assert not _ignored(negated, "packages/p/node_modules/NESTED_NODE")
    assert _ignored(negated, "node_modules/next/dist/bin/next")


def test_dockerignore_backend_effective_rules():
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for artifact in (".venv/Lib/site-packages/x.py", "data/trustflow.db"):
        assert _ignored(rules, artifact), artifact
    for required in ("pyproject.toml", "README.md",
                     "src/trustflow/api/app.py", "Dockerfile"):
        assert not _ignored(rules, required), required


def test_compose_publishes_loopback_only():
    import re

    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    ports = re.findall(r'^\s*-\s*"([^"]+)"', text, flags=re.MULTILINE)
    published = [p for p in ports if p.count(":") >= 2]
    assert published, "no published ports found"
    assert all(p.startswith("127.0.0.1:") for p in published), published
