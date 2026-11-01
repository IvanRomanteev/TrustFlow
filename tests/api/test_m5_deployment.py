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


def _lifecycle_handler(module, *, inspect_mode="found", rmi_rc=0,
                       write_iid=True, iid_map=None):
    """Fake Docker boundary emulating untagged builds via iidfile."""
    from pathlib import Path as _Path

    labels_by_id: dict = {}

    def handler(args):
        if args[:2] == ["docker", "build"]:
            iid_path = args[args.index("--iidfile") + 1]
            labels = {}
            for i, token in enumerate(args):
                if token == "--label":
                    key, value = args[i + 1].split("=", 1)
                    labels[key] = value
            image_id = (iid_map or {}).get(
                _Path(iid_path).name, _IMG)
            labels_by_id[image_id] = labels
            if write_iid:
                _Path(iid_path).write_text(image_id, encoding="utf-8")
            return _cp(0)
        if args[:3] == ["docker", "image", "inspect"]:
            if inspect_mode == "error":
                return _cp(1, "", "daemon unavailable")
            labels = dict(labels_by_id.get(args[3], {}))
            if inspect_mode == "wrong-label":
                labels[module.RUN_LABEL] = "someone-else"
            return _found(out_id=args[3], labels=labels)
        if args[:2] == ["docker", "rmi"]:
            return _cp(rmi_rc, "", "conflict" if rmi_rc else "")
        if args[:2] == ["docker", "run"]:
            return _cp(0, "/ctx/package.json /ctx/package-lock.json "
                          "/ctx/app/page.tsx /ctx/lib/api.ts")
        return _cp(0)

    return handler


def test_untagged_build_uses_iidfile_and_never_tags(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    calls = _scripted(module, monkeypatch, _lifecycle_handler(module))
    with module.ImageLifecycle() as lifecycle:
        image_id = lifecycle.build_image(
            module._tiny_context(tmp_path, "tiny"), "tiny")
    assert image_id == _IMG
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert builds and "--iidfile" in builds[0]
    assert calls[0][:2] == ["docker", "build"]  # no precheck inspect
    flat = [token for call in calls for token in call]
    assert "-t" not in flat
    assert not any(c[:2] == ["docker", "tag"] for c in calls)
    removals = [c for c in calls if c[:2] == ["docker", "rmi"]]
    assert removals == [["docker", "rmi", _IMG]]


def test_lifecycle_invalid_or_missing_iidfile_fails_closed(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    for kwargs in ({"write_iid": False},):
        calls = _scripted(module, monkeypatch,
                          _lifecycle_handler(module, **kwargs))
        with pytest.raises(AssertionError):
            with module.ImageLifecycle() as lifecycle:
                lifecycle.build_image(
                    module._tiny_context(tmp_path, "a"), "a")
        assert not any(c[:2] == ["docker", "rmi"] for c in calls)
    bad = _lifecycle_handler(module)
    def garbage(args):
        if args[:2] == ["docker", "build"]:
            from pathlib import Path as _p
            _p(args[args.index("--iidfile") + 1]).write_text("junk")
            return _cp(0)
        return bad(args)
    _scripted(module, monkeypatch, garbage)
    with pytest.raises(AssertionError):
        with module.ImageLifecycle() as lifecycle:
            lifecycle.build_image(
                module._tiny_context(tmp_path, "b"), "b")


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


def test_inspect_absence_shaped_output_is_error(monkeypatch):
    # Even a byte-exact absence diagnostic cannot prove absence:
    # Engine HTTP 404 and HTTP 500 produce identical CLI output.
    module = _docker_e2e_module()

    def handler(args):
        return _cp(1, "[]", f"Error response from daemon: "
                            f"No such image: {args[3]}")

    _scripted(module, monkeypatch, handler)
    res = module._inspect_image("repo:missing")
    assert res.state is module.InspectState.ERROR


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


def test_inspect_timeout_is_conservative_error(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        raise _sp.TimeoutExpired("docker", 120)

    calls = _scripted(module, monkeypatch, handler)
    assert module._inspect_image("repo:x").state is module.InspectState.ERROR
    assert _inspect_only(calls)  # nothing was authorized or mutated


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
    # Even the byte-exact genuine-absence text is only ERROR: the CLI
    # cannot distinguish Engine 404 from 500 (R4 invariant).
    _scripted(module, monkeypatch, lambda args: _cp(1, "[]", exact))
    assert module._inspect_image(ref).state is module.InspectState.ERROR


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


def test_post_build_inspection_failure_reports_id_and_detail(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    _scripted(module, monkeypatch,
              _lifecycle_handler(module, inspect_mode="error"))
    with pytest.raises(ExceptionGroup) as excinfo:
        module._run_matcher_build(tmp_path)
    cause = str(excinfo.value.__cause__ or "")
    assert _IMG in cause and "ownership unknown" in cause


def test_lifecycle_primary_and_cleanup_failures_both_survive(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    _scripted(module, monkeypatch,
              _lifecycle_handler(module, rmi_rc=1))
    with pytest.raises(ExceptionGroup) as excinfo:
        with module.ImageLifecycle() as lifecycle:
            lifecycle.build_image(
                module._tiny_context(tmp_path, "x"), "x")
            raise ValueError("primary-marker")
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "primary-marker" in str(excinfo.value.__cause__)
    failures = excinfo.value.exceptions
    assert failures and failures[0].image_id == _IMG


def test_lifecycle_cleanup_failure_without_primary_fails(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    _scripted(module, monkeypatch,
              _lifecycle_handler(module, rmi_rc=1))
    with pytest.raises(ExceptionGroup) as excinfo:
        with module.ImageLifecycle() as lifecycle:
            lifecycle.build_image(
                module._tiny_context(tmp_path, "y"), "y")
    assert excinfo.value.exceptions[0].image_id == _IMG


def test_lifecycle_partial_creation_cleans_known_owned_only(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    calls = []
    iid_map: dict = {}
    real = _lifecycle_handler(module, iid_map=iid_map)

    def handler(args, **_kwargs):
        calls.append(list(args))
        if args[:2] == ["docker", "build"]:
            from pathlib import Path as _p
            name = _p(args[args.index("--iidfile") + 1]).name
            iid_map[name] = _OTHER if name.endswith("-b") else _IMG
        if args[:3] == ["docker", "image", "inspect"] \
                and args[3] == _OTHER:
            return _cp(1, "", "daemon unavailable")
        return real(args)

    monkeypatch.setattr(module, "_run", handler)
    with pytest.raises(ExceptionGroup):
        with module.ImageLifecycle() as lifecycle:
            lifecycle.build_image(
                module._tiny_context(tmp_path, "a"), "a")
            lifecycle.build_image(
                module._tiny_context(tmp_path, "b"), "b")
    removals = [c for c in calls if c[:2] == ["docker", "rmi"]]
    assert removals == [["docker", "rmi", _IMG]]  # B never targeted


def test_retag_attack_mocked_preserves_full_mapping(monkeypatch):
    module = _docker_e2e_module()
    import copy as _copy

    state = {
        _IMG: {"labels": {module.RUN_LABEL: "run-a"},
               "tags": {"repo:run-a", "repo:extra"}},
        _OTHER: {"labels": {module.RUN_LABEL: "run-b"},
                 "tags": {"repo:b-original"}},
    }
    before_b = _copy.deepcopy(state[_OTHER])

    def handler(args):
        if args[:3] == ["docker", "image", "inspect"]:
            entry = state[args[3]]
            return _cp(0, _json.dumps([{
                "Id": args[3],
                "Config": {"Labels": entry["labels"]},
                "RepoTags": sorted(entry["tags"])}]))
        if args[:2] == ["docker", "rmi"]:
            assert args[2] in state  # only recorded IDs may be removed
            del state[args[2]]
            return _cp(0)
        return _cp(0)

    calls = _scripted(module, monkeypatch, handler)
    # Simulated retag attack: mappings move, IDs/labels do not.
    state[_OTHER]["tags"].add("repo:extra")
    state[_IMG]["tags"].discard("repo:extra")
    expected_b = {"labels": before_b["labels"],
                  "tags": {"repo:b-original", "repo:extra"}}
    outcome = module._cleanup_owned_image(_IMG, "run-a")
    assert outcome == "removed"  # A addressed only by its recorded ID
    assert state[_OTHER] == expected_b  # B ID/labels/tags fully intact
    assert not any(c[:2] == ["docker", "tag"] for c in calls)
    assert [c for c in calls if c[:2] == ["docker", "rmi"]] == [
        ["docker", "rmi", _IMG]]


def test_compose_teardown_failure_is_enforceable():
    module = _docker_e2e_module()
    failure = module.CleanupFailure(
        "compose-down", None, "run-x", "docker compose down -v",
        "rc=1", preserved=True)
    with pytest.raises(ExceptionGroup):
        module._raise_stack_result(None, [failure])
    with pytest.raises(ExceptionGroup) as excinfo:
        module._raise_stack_result(ValueError("primary"), [failure])
    assert isinstance(excinfo.value.__cause__, ValueError)
    with pytest.raises(ValueError):
        module._raise_stack_result(ValueError("primary"), [])
    module._raise_stack_result(None, [])  # must not raise


def test_compose_preflight_rejects_occupied_namespace(monkeypatch):
    module = _docker_e2e_module()

    def handler(args):
        if args[:2] == ["docker", "ps"]:
            return _cp(0, "deadbeefcafe\n")
        return _cp(0, "")

    calls = _scripted(module, monkeypatch, handler)
    with pytest.raises(module.NamespaceConflict):
        module._compose_preflight("tf-m5-it-forced")
    # Only read-only namespace queries ran: no up/down/build mutation.
    assert calls and all(
        c[1] in ("ps", "image", "network", "volume") for c in calls)
    assert not any("compose" in c for c in calls)


def test_compose_teardown_timeout_aggregates_with_primary(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    run = module._StackRun(tmp_path)
    run.ownership_verified = True

    def handler(args, **_kwargs):
        if args[:3] == ["docker", "network", "ls"]:
            raise _sp.TimeoutExpired(args, 120)
        return _cp(0, "")

    monkeypatch.setattr(module, "_run", handler)
    failures = run.teardown()
    assert failures, "inventory timeout must become a cleanup failure"
    assert any("TimeoutExpired" in str(f) for f in failures)
    with pytest.raises(ExceptionGroup) as excinfo:
        module._raise_stack_result(
            AssertionError("PRIMARY_MARKER"), failures)
    assert "PRIMARY_MARKER" in str(excinfo.value.__cause__)
    assert any(f in excinfo.value.exceptions for f in failures[:1])


def test_compose_discovery_classifies_foreign_resources(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    run = module._StackRun(tmp_path)
    project, run_id = run.project, run.run_id

    def handler(args, **_kwargs):
        if args[:2] == ["docker", "ps"]:
            return _cp(0, "c" * 64 + "\n" + "d" * 64 + "\n")
        if args[:3] == ["docker", "network", "ls"]:
            return _cp(0, "e" * 64 + "\n")
        if args[:2] == ["docker", "inspect"]:
            if "c" * 64 in args:
                return _cp(0, _json.dumps([{
                    "Id": "c" * 64, "Name": "/own",
                    "Config": {"Labels": {
                        module.PROJECT_LABEL: project,
                        module.RUN_LABEL: run_id}},
                    "State": {"Running": False}}]))
            if "d" * 64 in args:
                return _cp(0, _json.dumps([{
                    "Id": "d" * 64, "Name": "/foreign",
                    "Config": {"Labels": {
                        module.PROJECT_LABEL: project,
                        module.RUN_LABEL: "other-run"}},
                    "State": {"Running": False}}]))
            return _cp(0, _json.dumps([{
                "Id": "e" * 64, "Name": "n",
                "Labels": {module.PROJECT_LABEL: project,
                           module.RUN_LABEL: "other-run"}}]))
        return _cp(0, "")

    monkeypatch.setattr(module, "_run", handler)
    log = module._FailureLog(run.run_id)
    owned, foreign = run._discover(log)
    assert not log.items
    assert [r["id"] for r in owned["containers"]] == ["c" * 64]
    assert {r["id"] for r in foreign} == {"d" * 64, "e" * 64}


def test_compose_discovery_malformed_metadata_fails_closed(
        monkeypatch, tmp_path):
    """F3/F4 unit: malformed volume labels become a structured
    failure in the accumulator, never a silently discarded record."""
    module = _docker_e2e_module()
    run = module._StackRun(tmp_path)

    def handler(args, **_kwargs):
        if args[:3] == ["docker", "volume", "ls"]:
            return _cp(0, "volbad\n")
        if args[:2] == ["docker", "inspect"]:
            return _cp(0, _json.dumps([{
                "Name": "volbad", "Labels": ["bad"]}]))
        return _cp(0, "")

    monkeypatch.setattr(module, "_run", handler)
    log = module._FailureLog(run.run_id)
    owned, foreign = run._discover(log)
    assert owned["volumes"] == [] and foreign == []
    assert log.items and "malformed" in str(log.items[0])


def test_teardown_never_removes_volumes_by_name(monkeypatch, tmp_path):
    """F2 unit: even a verified-owned named volume is preserved and
    reported; teardown never issues `docker volume rm`."""
    module = _docker_e2e_module()
    run = module._StackRun(tmp_path)
    run.ownership_verified = True
    calls: list = []

    def handler(args, **_kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "ls"]:
            return _cp(0, "vol-own\n")
        if argv[:2] == ["docker", "inspect"]:
            return _cp(0, _json.dumps([{
                "Name": "vol-own",
                "Labels": {module.PROJECT_LABEL: run.project,
                           module.RUN_LABEL: run.run_id}}]))
        return _cp(0, "")

    monkeypatch.setattr(module, "_run", handler)
    failures = run.teardown()
    assert failures and any(
        "volumes are never deleted" in str(f) for f in failures)
    assert not any(a[:3] == ["docker", "volume", "rm"] for a in calls)


def test_stack_full_function_preserves_all_failures(
        monkeypatch, tmp_path):
    """N3/F4 graph: primary + inventory + malformed + discovery
    failures all coexist in one accumulated failure graph."""
    module = _docker_e2e_module()
    state: dict = {"run_id": None, "project": None,
                   "build_seen": False, "bind_source": None}

    def handler(args, **_kwargs):
        import re as _re
        from pathlib import Path as _p

        argv = list(args)
        if argv[:2] == ["docker", "compose"]:
            if "-p" in argv:
                state["project"] = argv[argv.index("-p") + 1]
            for token in argv:
                if "override-" in token and token.endswith(".yml"):
                    text = _p(token).read_text()
                    match = _re.search(r"trustflow\.test-run=([0-9a-f]+)",
                                       text)
                    if match:
                        state["run_id"] = match.group(1)
                    source = _re.search(r"source:\s*(\S+)", text)
                    if source:
                        state["bind_source"] = source.group(1)
            if "build" in argv:
                state["build_seen"] = True
                return _cp(1, "", "PRIMARY_SETUP_MARKER")
            if "config" in argv:
                return _cp(0, _json.dumps({"services": {"backend": {
                    "volumes": [{"type": "bind",
                                 "source": state["bind_source"],
                                 "target": "/app"}]}}}))
            return _cp(0, "")
        if argv[:2] == ["docker", "ps"]:
            return _cp(0, "")
        if argv[:3] == ["docker", "image", "ls"]:
            if state["build_seen"] and "--filter" in argv:
                return _cp(0, _IMG + "\n")
            return _cp(0, "")
        if argv[:3] == ["docker", "network", "ls"]:
            if state["build_seen"]:
                return _cp(1, "", "NETWORK_INVENTORY_DENIED_MARKER")
            return _cp(0, "")
        if argv[:3] == ["docker", "volume", "ls"]:
            return _cp(0, "volbad\n") if state["build_seen"] \
                else _cp(0, "")
        if argv[:2] == ["docker", "inspect"]:
            return _cp(0, _json.dumps([{
                "Name": "volbad", "Labels": ["bad"]}]))
        if argv[:3] == ["docker", "image", "inspect"]:
            raise PermissionError("INSPECT_PERMISSION_MARKER")
        return _cp(0, "")

    monkeypatch.setattr(module, "_run", handler)
    with pytest.raises(BaseException) as excinfo:
        module._run_stack_test(tmp_path)
    full = module._exception_graph_text(excinfo.value)
    assert "PRIMARY_SETUP_MARKER" in full
    assert "NETWORK_INVENTORY_DENIED_MARKER" in full
    assert "malformed" in full
    assert "INSPECT_PERMISSION_MARKER" in full


def test_compose_postcondition_catches_rc0_leftover_image(
        monkeypatch, tmp_path):
    module = _docker_e2e_module()
    run = module._StackRun(tmp_path)
    run.ownership_verified = True
    run.image_ids = {"backend": _IMG}

    def handler(args, **_kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return _found(labels={module.RUN_LABEL: run.run_id})
        return _cp(0, "")  # down and rmi both claim success

    monkeypatch.setattr(module, "_run", handler)
    failures = run.teardown()
    # rc=0 everywhere, yet the owned image is still present: the
    # postcondition (not the exit code) decides mandatory cleanup.
    assert failures
    assert any(_IMG in str(f) for f in failures)


def test_no_mutable_tag_operations_in_integration_source():
    source = (ROOT / "tests" / "integration"
              / "test_m5_docker_e2e.py").read_text(encoding="utf-8")
    assert '"docker", "tag"' not in source
    assert '", "-t"' not in source and '"-t"]' not in source
    assert "--iidfile" in source


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
