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


# --- P2-05: matcher image ownership helpers (pure logic, no daemon) ---


def _docker_e2e_module():
    import importlib.util

    path = ROOT / "tests" / "integration" / "test_m5_docker_e2e.py"
    spec = importlib.util.spec_from_file_location("tf_m5_docker_e2e", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matcher_image_tags_are_unique_and_never_the_fixed_tag():
    module = _docker_e2e_module()
    tags = {module._unique_image_tag() for _ in range(8)}
    assert len(tags) == 8  # (tag, run_id) pairs all distinct
    for tag, run_id in tags:
        assert tag == f"trustflow-m5-matcher-test:{run_id}"
        assert tag != module.LEGACY_FIXED_TAG
        assert len(run_id) == 32  # uuid4 hex: collision-resistant


def test_owned_image_cleanup_requires_label_and_identity():
    module = _docker_e2e_module()
    labels = {module.RUN_LABEL: "run-1"}
    assert module._owned_image_ok(labels, "sha256:a", "run-1", "sha256:a")
    assert not module._owned_image_ok(labels, "sha256:b", "run-1", "sha256:a")
    assert not module._owned_image_ok(labels, "sha256:a", "run-2", "sha256:a")
    assert not module._owned_image_ok({}, "sha256:a", "run-1", "sha256:a")
    assert not module._owned_image_ok(None, "sha256:a", "run-1", "sha256:a")
    assert not module._owned_image_ok(labels, None, "run-1", None)


# --- P2-05 R2 adversarial matrix (fake Docker runner, no daemon) ------
# Each case scripts the Docker CLI boundary and asserts the observable
# safety outcome: no mutation is ever authorized by an inconclusive
# inspection, and cleanup addresses only the recorded immutable ID.

import json as _json  # noqa: E402
import subprocess as _sp  # noqa: E402

_IMG = "sha256:" + "a" * 64
_OTHER = "sha256:" + "b" * 64


def _cp(rc, out="", err=""):
    return _sp.CompletedProcess([], rc, out, err)


def _found(out_id=_IMG, labels=None):
    return _cp(0, _json.dumps(
        [{"Id": out_id, "Config": {"Labels": labels or {}}}]))


def _scripted(module, monkeypatch, handler):
    calls = []

    def fake_run(args, timeout=600, cwd=None):
        calls.append(list(args))
        return handler(list(args))

    monkeypatch.setattr(module, "_run", fake_run)
    return calls


def _inspect_only(calls):
    return all(c[:3] == ["docker", "image", "inspect"] for c in calls)


def test_inspect_confirmed_missing_is_not_found(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        return _cp(1, "[]", f"Error response from daemon: "
                            f"No such image: {args[3]}")

    _scripted(module, monkeypatch, handler)
    res = module._inspect_image("repo:missing")
    assert res.state is module.InspectState.NOT_FOUND


def test_inspect_found_validates_metadata(monkeypatch):
    module = _docker_e2e_module()
    _scripted(module, monkeypatch,
              lambda args: _found(labels={module.RUN_LABEL: "r1"}))
    res = module._inspect_image(_IMG)
    assert res.state is module.InspectState.FOUND
    assert res.image_id == _IMG
    assert res.labels[module.RUN_LABEL] == "r1"


def test_inspect_daemon_permission_and_unknown_errors_fail_closed(monkeypatch):
    module = _docker_e2e_module()
    for err in ("Cannot connect to the Docker daemon at unix:///x",
                "permission denied while trying to connect to Docker",
                "unknown flag: --bogus"):
        _scripted(module, monkeypatch,
                  lambda args, e=err: _cp(1, "", e))
        res = module._inspect_image("repo:x")
        assert res.state is module.InspectState.ERROR, err


def test_inspect_malformed_and_empty_output_fail_closed(monkeypatch):
    module = _docker_e2e_module()
    for out in ("not-json", "[]", "{}", '[{"Id": 42}]'):
        _scripted(module, monkeypatch, lambda args, o=out: _cp(0, o))
        assert module._inspect_image("repo:x").state is \
            module.InspectState.ERROR, out


def test_inspect_timeout_is_error_and_blocks_allocation(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        raise _sp.TimeoutExpired("docker", 120)

    calls = _scripted(module, monkeypatch, handler)
    assert module._inspect_image("repo:x").state is module.InspectState.ERROR
    with pytest.raises(AssertionError):
        module._allocate_matcher_tag()
    assert _inspect_only(calls)  # no build/mutation was attempted


def test_allocation_aborts_on_inspection_error(monkeypatch):
    module = _docker_e2e_module()
    calls = _scripted(module, monkeypatch,
                       lambda args: _cp(1, "", "daemon unavailable"))
    with pytest.raises(AssertionError):
        module._allocate_matcher_tag()
    assert _inspect_only(calls)


def test_sentinel_guard_fails_closed_on_error():
    module = _docker_e2e_module()
    err = module.InspectResult(module.InspectState.ERROR, detail="boom")
    with pytest.raises(AssertionError):
        module._fail_if_error(err, "legacy")
    ok = module.InspectResult(module.InspectState.NOT_FOUND)
    module._fail_if_error(ok, "legacy")  # must not raise


def test_cleanup_inspection_error_is_visible_failure(monkeypatch):
    module = _docker_e2e_module()
    calls = _scripted(module, monkeypatch,
                       lambda args: _cp(1, "", "daemon unavailable"))
    with pytest.warns(UserWarning):
        outcome = module._cleanup_owned_image(_IMG, "run-1")
    assert outcome == "cleanup-failed"  # never a silent success
    assert not any("rmi" in c for c in calls)


def test_cleanup_label_and_id_mismatch_preserve(monkeypatch):
    module = _docker_e2e_module()
    for found in (_found(labels={module.RUN_LABEL: "other"}),
                  _found(out_id=_OTHER, labels={module.RUN_LABEL: "run-1"})):
        calls = _scripted(module, monkeypatch, lambda args, f=found: f)
        with pytest.warns(UserWarning):
            outcome = module._cleanup_owned_image(_IMG, "run-1")
        assert outcome == "preserved-unverified"
        assert _inspect_only(calls)


def test_cleanup_without_recorded_identity_makes_no_docker_calls(monkeypatch):
    module = _docker_e2e_module()
    calls = _scripted(module, monkeypatch, lambda args: _cp(0))
    with pytest.warns(UserWarning):
        assert module._cleanup_owned_image(None, None) == "preserved-unverified"
    assert calls == []


def test_cleanup_removes_by_immutable_id_only(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        if args[:3] == ["docker", "image", "inspect"]:
            return _found(labels={module.RUN_LABEL: "run-1"})
        return _cp(0)

    calls = _scripted(module, monkeypatch, handler)
    assert module._cleanup_owned_image(_IMG, "run-1") == "removed"
    removals = [c for c in calls if "rmi" in c]
    assert removals == [["docker", "rmi", _IMG]]  # ID target, no -f, no tag


def test_inspect_absence_requires_exact_full_message(monkeypatch):
    module = _docker_e2e_module()
    ref = "trustflow-m5-matcher-test:abc123"
    exact = f"Error response from daemon: No such image: {ref}"
    variants = {
        "quoted": f'Error response from daemon: upstream failed '
                  f'while processing "No such image: {ref}"',
        "mixed": exact + "\npermission denied while trying to connect",
        "wrong-suffix": f"Error response from daemon: "
                        f"No such image: {ref}-other",
        "prefix": "Error response from daemon: No such image: "
                  "trustflow-m5-matcher-test:abc",
        "extra-line": exact + "\nError: additional failure",
        "http500-embedded": f"Error response from daemon: 500 Internal "
                            f'Server Error for "No such image: {ref}"',
    }
    for name, err in variants.items():
        _scripted(module, monkeypatch,
                  lambda args, e=err: _cp(1, "[]", e))
        res = module._inspect_image(ref)
        assert res.state is module.InspectState.ERROR, name
    _scripted(module, monkeypatch, lambda args: _cp(1, "[]", exact))
    assert module._inspect_image(ref).state is module.InspectState.NOT_FOUND


def test_cleanup_false_absence_cannot_report_absent(monkeypatch):
    module = _docker_e2e_module()
    quoted = (f'Error response from daemon: backend failure while '
              f'processing "No such image: {_IMG}"')
    calls = _scripted(module, monkeypatch, lambda args: _cp(1, "[]", quoted))
    with pytest.warns(UserWarning):
        outcome = module._cleanup_owned_image(_IMG, "run-1")
    assert outcome == "cleanup-failed"  # never a false "absent" success
    assert not any("rmi" in c for c in calls)


def test_inspect_invalid_metadata_shapes_fail_closed(monkeypatch):
    module = _docker_e2e_module()
    good_labels = {module.RUN_LABEL: "r"}
    payloads = {
        "config-string": [{"Id": _IMG, "Config": "nope"}],
        "config-list": [{"Id": _IMG, "Config": []}],
        "labels-list": [{"Id": _IMG, "Config": {"Labels": []}}],
        "labels-int": [{"Id": _IMG, "Config": {"Labels": 0}}],
        "labels-nonstring-value": [
            {"Id": _IMG, "Config": {"Labels": {"k": 1}}}],
        "truncated-id": [{"Id": "sha256:a", "Config": {"Labels": {}}}],
        "nonhex-id": [
            {"Id": "sha256:" + "g" * 64, "Config": {"Labels": {}}}],
        "missing-id": [{"Config": {"Labels": good_labels}}],
        "missing-config": [{"Id": _IMG}],
    }
    for name, payload in payloads.items():
        _scripted(module, monkeypatch,
                  lambda args, p=payload: _cp(0, _json.dumps(p)))
        res = module._inspect_image(_IMG)
        assert res.state is module.InspectState.ERROR, name


def test_cleanup_removal_timeout_propagates_visibly(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        if args[:3] == ["docker", "image", "inspect"]:
            return _found(labels={module.RUN_LABEL: "run-1"})
        raise _sp.TimeoutExpired("docker", 120)

    _scripted(module, monkeypatch, handler)
    with pytest.raises(_sp.TimeoutExpired):
        module._cleanup_owned_image(_IMG, "run-1")


def test_post_build_inspection_failure_reports_tag_and_detail(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()

    def handler(args):
        if args[:3] == ["docker", "image", "inspect"]:
            ref = args[3]
            if ref.startswith("sha256:"):
                return _cp(1, "", "daemon unavailable")
            return _cp(1, "[]", f"Error response from daemon: "
                                f"No such image: {ref}")
        return _cp(0)  # build/run succeed; inspection then fails

    _scripted(module, monkeypatch, handler)
    with pytest.raises(AssertionError) as excinfo:
        module._run_matcher_build(tmp_path)
    message = str(excinfo.value)
    assert "trustflow-m5-matcher-test:" in message
    assert "ownership unknown" in message


def test_fixture_builder_reports_tag_on_inspection_failure(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()

    def handler(args):
        if args[:3] == ["docker", "image", "inspect"]:
            return _cp(1, "", "permission denied while trying to connect")
        return _cp(0)

    _scripted(module, monkeypatch, handler)
    with pytest.raises(AssertionError) as excinfo:
        module._build_labeled_image(
            tmp_path, "sentinel", {module.RUN_LABEL: "run-x"})
    message = str(excinfo.value)
    assert "trustflow-m5-sentinel:run-x" in message
    assert "ownership unknown" in message


def test_cleanup_removal_refusal_is_visible(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        if args[:3] == ["docker", "image", "inspect"]:
            return _found(labels={module.RUN_LABEL: "run-1"})
        return _cp(1, "", "conflict: unable to delete (must be forced)")

    _scripted(module, monkeypatch, handler)
    with pytest.warns(UserWarning):
        outcome = module._cleanup_owned_image(_IMG, "run-1")
    assert outcome == "preserved-conflict"


def test_compose_publishes_loopback_only():
    import re

    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    ports = re.findall(r'^\s*-\s*"([^"]+)"', text, flags=re.MULTILINE)
    published = [p for p in ports if p.count(":") >= 2]
    assert published, "no published ports found"
    assert all(p.startswith("127.0.0.1:") for p in published), published
