"""Opt-in Docker integration gates for M5.1 (P1-01, P2-02, P3-04).

SKIPPED unless TRUSTFLOW_DOCKER_TESTS=1. A skip is never a pass; these
gates need a running Docker daemon. Run explicitly:

    $env:TRUSTFLOW_DOCKER_TESTS="1"
    .\\.venv\\Scripts\\python.exe -m pytest tests/integration -v

The stack test uses an isolated Compose project, free loopback ports
(the corrected host_ip is preserved in the override) and a fresh
named volume, and removes only its own project resources afterwards.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TRUSTFLOW_DOCKER_TESTS") != "1",
    reason="opt-in Docker gate: set TRUSTFLOW_DOCKER_TESTS=1",
)

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "demo-approval-token"


def _run(args: list[str], timeout: int = 600,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, cwd=cwd or ROOT)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(method: str, url: str, body: dict | None = None,
          headers: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw) if raw else {}
            except ValueError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {}


def test_compose_default_config_binds_loopback_only():
    proc = _run(["docker", "compose", "config", "--format", "json"], timeout=120)
    assert proc.returncode == 0, proc.stderr
    config = json.loads(proc.stdout)
    for service in ("backend", "frontend"):
        ports = config["services"][service]["ports"]
        assert ports, service
        for port in ports:
            assert port.get("host_ip") == "127.0.0.1", (service, port)


# --- Fail-closed inspection + immutable-ID cleanup (P2-05 R2) --------
# R1 defects closed here: (R1) inspect errors were mapped to "missing"
# and authorized overwriting/deleting existing images; (R2) cleanup
# removed a mutable tag after validating a different image ID (retag
# TOCTOU). Inspection is now an explicit three-state result, and
# deletion addresses only the recorded immutable image ID.

RUN_LABEL = "trustflow.test-run"
SENTINEL_LABEL = "trustflow.sentinel"
LEGACY_FIXED_TAG = "trustflow-m5-matcher-test:latest"  # never written


class InspectState(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class InspectResult:
    state: InspectState
    image_id: str | None = None
    labels: dict = field(default_factory=dict)
    detail: str = ""


def _inspect_image(ref: str) -> InspectResult:
    """Three-state image inspection; never guesses absence.

    NOT_FOUND requires every confirmed-absence signal the real Docker
    CLI produces (nonzero exit, stdout exactly ``[]``, and the
    daemon's ``No such image: <ref>`` message). Daemon, permission,
    transport, timeout and malformed-output failures are ERROR.
    """
    try:
        proc = _run(["docker", "image", "inspect", ref], timeout=120)
    except subprocess.TimeoutExpired:
        return InspectResult(InspectState.ERROR,
                             detail=f"inspect timed out for {ref}")
    if proc.returncode == 0:
        try:
            items = json.loads(proc.stdout)
        except ValueError:
            return InspectResult(InspectState.ERROR,
                                 detail=f"malformed inspect output for {ref}")
        if not isinstance(items, list) or len(items) != 1 \
                or not isinstance(items[0], dict):
            return InspectResult(InspectState.ERROR,
                                 detail=f"unexpected inspect result for {ref}")
        image_id = items[0].get("Id")
        labels = (items[0].get("Config") or {}).get("Labels") or {}
        if not isinstance(image_id, str) \
                or not image_id.startswith("sha256:") \
                or not isinstance(labels, dict):
            return InspectResult(InspectState.ERROR,
                                 detail=f"invalid inspect metadata for {ref}")
        return InspectResult(InspectState.FOUND, image_id=image_id,
                             labels=dict(labels))
    if (proc.stdout or "").strip() == "[]" \
            and f"No such image: {ref}" in (proc.stderr or ""):
        return InspectResult(InspectState.NOT_FOUND)
    return InspectResult(
        InspectState.ERROR,
        detail=f"inspect rc={proc.returncode} for {ref}: "
               f"{(proc.stderr or '').strip()[:300]}")


def _unique_image_tag() -> tuple[str, str]:
    run_id = uuid.uuid4().hex
    return f"trustflow-m5-matcher-test:{run_id}", run_id


def _unique_sentinel_tag() -> tuple[str, str]:
    run_id = uuid.uuid4().hex
    return f"trustflow-m5-sentinel:{run_id}", run_id


def _owned_image_ok(labels: dict | None, image_id: str | None,
                    run_id: str, expected_id: str | None) -> bool:
    return bool(image_id) and image_id == expected_id and bool(labels) \
        and labels.get(RUN_LABEL) == run_id


def _fail_if_error(res: InspectResult, what: str) -> None:
    """Fail closed when a safety-relevant inspection is inconclusive."""
    if res.state is InspectState.ERROR:
        raise AssertionError(f"{what}: inspection ERROR: {res.detail}")


def _allocate_matcher_tag() -> tuple[str, str]:
    """Allocate a tag only on confirmed NOT_FOUND; ERROR aborts."""
    for _attempt in range(5):
        tag, run_id = _unique_image_tag()
        res = _inspect_image(tag)
        if res.state is InspectState.NOT_FOUND:
            return tag, run_id
        if res.state is InspectState.ERROR:
            raise AssertionError(
                f"tag allocation aborted before build: {res.detail}")
    raise AssertionError("could not allocate a non-existing image tag")


def _cleanup_owned_image(image_id: str | None, run_id: str | None) -> str:
    """Delete only the recorded immutable image ID (never a tag).

    Outcomes: "removed" / "absent" / "preserved-unverified" /
    "preserved-conflict" / "cleanup-failed". Ownership is verified
    against the image addressed by its own ID, so retagging any
    mutable tag cannot redirect deletion. Docker refusals (multiple
    tags, containers, conflicts) preserve the image; no force is
    ever used. A removal timeout propagates visibly.
    """
    if not image_id or not run_id:
        warnings.warn(f"P2-05: preserving image with unrecorded identity "
                      f"(id={image_id}, run={run_id})", stacklevel=2)
        return "preserved-unverified"
    res = _inspect_image(image_id)
    if res.state is InspectState.NOT_FOUND:
        return "absent"
    if res.state is InspectState.ERROR:
        warnings.warn(f"P2-05: cleanup inspection failed for "
                      f"{image_id}: {res.detail}", stacklevel=2)
        return "cleanup-failed"
    if not _owned_image_ok(res.labels, res.image_id, run_id, image_id):
        warnings.warn(f"P2-05: preserving unverified image {image_id} "
                      f"(labels={res.labels})", stacklevel=2)
        return "preserved-unverified"
    proc = _run(["docker", "rmi", image_id], timeout=120)  # ID, no -f
    if proc.returncode != 0:
        warnings.warn(f"P2-05: preserving {image_id}, docker refused "
                      f"removal: {(proc.stderr or '').strip()[:300]}",
                      stacklevel=2)
        return "preserved-conflict"
    return "removed"


def _run_matcher_build(tmp_path: Path) -> tuple[str, str, set[str]]:
    """Build the poisoned-context fixture into a unique owned image.

    Returns (tag, run_id, listed paths). The image is cleaned up
    (ownership-verified) before returning; tag/run_id identify what
    this invocation created.
    """
    ctx = tmp_path / "ctx"
    fixtures = ["node_modules/next/dist/bin/next", ".next/BUILD_ID",
                "packages/p/node_modules/NESTED_NODE",
                "packages/p/.next/NESTED_NEXT",
                "packages/p/npm-debug.log.1",
                "package.json", "package-lock.json",
                "app/page.tsx", "lib/api.ts"]
    for rel in fixtures:
        path = ctx / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    shutil.copy(ROOT / "frontend" / ".dockerignore", ctx / ".dockerignore")
    (ctx / "Dockerfile").write_text(
        "FROM node:22-alpine\nCOPY . /ctx\n"
        "RUN find /ctx -type f | sort > /listing.txt\n", encoding="utf-8")
    tag, run_id = _allocate_matcher_tag()  # aborts on inspect ERROR
    image_id: str | None = None
    result: tuple[str, str, set[str]] | None = None
    primary: BaseException | None = None
    try:
        build = _run(["docker", "build", "--label", f"{RUN_LABEL}={run_id}",
                      "-t", tag, str(ctx)])
        assert build.returncode == 0, build.stderr[-2000:]
        created = _inspect_image(tag)
        if created.state is not InspectState.FOUND:
            raise AssertionError(
                f"post-build inspection {created.state.value} for {tag}: "
                f"{created.detail}; image preserved under that tag")
        image_id = created.image_id
        if created.labels.get(RUN_LABEL) != run_id:
            raise AssertionError(
                f"ownership label mismatch on {image_id}: {created.labels}")
        out = _run(["docker", "run", "--rm", "--entrypoint", "cat",
                    tag, "/listing.txt"])
        assert out.returncode == 0, out.stderr
        result = (tag, run_id, set(out.stdout.split()))
    except BaseException as exc:  # cleanup below must not mask it
        primary = exc
    outcome = _cleanup_owned_image(image_id, run_id)
    if primary is not None:
        if outcome not in ("removed", "absent"):
            warnings.warn(f"P2-05: matcher cleanup outcome={outcome} "
                          f"for image_id={image_id} tag={tag}", stacklevel=2)
        raise primary
    if outcome not in ("removed", "absent"):
        raise AssertionError(
            f"matcher cleanup outcome={outcome} for image_id={image_id} "
            f"tag={tag}; image preserved for controlled cleanup")
    assert result is not None
    return result


def _assert_matcher_listing(listed: set[str]) -> None:
    for artifact in ("node_modules", ".next", "NESTED_NODE", "NESTED_NEXT",
                     "npm-debug.log.1"):
        assert not any(artifact in p for p in listed), (artifact, listed)
    for required in ("/ctx/package.json", "/ctx/package-lock.json",
                     "/ctx/app/page.tsx", "/ctx/lib/api.ts"):
        assert required in listed, listed


def test_real_docker_matcher_frontend_context(tmp_path):
    tag, _run_id, listed = _run_matcher_build(tmp_path)
    assert tag != LEGACY_FIXED_TAG
    _assert_matcher_listing(listed)


def _build_labeled_image(tmp_path: Path, name: str,
                         labels: dict[str, str]) -> tuple[str, str, str]:
    """Build a tiny test-owned image; returns (tag, run_id, image_id)."""
    ctx = tmp_path / name
    ctx.mkdir()
    label_lines = "".join(f'LABEL {k}="{v}"\n' for k, v in labels.items())
    (ctx / "Dockerfile").write_text(
        f"FROM node:22-alpine\n{label_lines}", encoding="utf-8")
    run_id = labels[RUN_LABEL]
    tag = f"trustflow-m5-sentinel:{run_id}"
    build = _run(["docker", "build", "-t", tag, str(ctx)])
    assert build.returncode == 0, build.stderr[-2000:]
    created = _inspect_image(tag)
    assert created.state is InspectState.FOUND and created.image_id
    return tag, run_id, created.image_id


def test_matcher_preserves_unrelated_images(tmp_path):
    """P2-05 R2 sentinel design: tests never write the legacy tag.

    A pre-existing legacy image is only inspected (identity recorded,
    never modified); unknown legacy state fails closed. The sentinel
    lives under a unique per-invocation tag and is cleaned up by ID.
    """
    legacy = _inspect_image(LEGACY_FIXED_TAG)
    _fail_if_error(legacy, "legacy tag state unknown")
    s_tag, s_run, s_id = _build_labeled_image(
        tmp_path, "sentinel",
        {RUN_LABEL: uuid.uuid4().hex, SENTINEL_LABEL: "m5-p2-05"})
    assert s_tag != LEGACY_FIXED_TAG
    try:
        tag, _run_id, listed = _run_matcher_build(tmp_path)
        assert tag != LEGACY_FIXED_TAG
        _assert_matcher_listing(listed)
        after_s = _inspect_image(s_id)
        assert after_s.state is InspectState.FOUND
        assert after_s.image_id == s_id
        assert after_s.labels.get(SENTINEL_LABEL) == "m5-p2-05"
        if legacy.state is InspectState.FOUND:
            after_l = _inspect_image(LEGACY_FIXED_TAG)
            assert after_l.state is InspectState.FOUND
            assert after_l.image_id == legacy.image_id
            assert after_l.labels == legacy.labels
    finally:
        outcome = _cleanup_owned_image(s_id, s_run)
        assert outcome in ("removed", "absent"), outcome


def test_inspect_states_against_real_docker(tmp_path):
    missing_tag, _run_id = _unique_image_tag()
    assert _inspect_image(missing_tag).state is InspectState.NOT_FOUND
    _tag, _run, image_id = _build_labeled_image(
        tmp_path, "found", {RUN_LABEL: uuid.uuid4().hex})
    try:
        found = _inspect_image(image_id)
        assert found.state is InspectState.FOUND
        assert found.image_id == image_id
        assert found.labels.get(RUN_LABEL)
    finally:
        assert _cleanup_owned_image(image_id, _run) in ("removed", "absent")


def test_retag_race_never_deletes_replacement(tmp_path):
    """R2 regression on real Docker: cleanup follows the recorded ID."""
    _ta, run_a, id_a = _build_labeled_image(
        tmp_path, "race-a", {RUN_LABEL: uuid.uuid4().hex})
    tag_b, run_b, id_b = _build_labeled_image(
        tmp_path, "race-b", {RUN_LABEL: uuid.uuid4().hex})
    run_tag = _ta
    try:
        # Controlled boundary: reassign the run tag to B, drop B's tag.
        assert _run(["docker", "tag", id_b, run_tag]).returncode == 0
        assert _run(["docker", "rmi", tag_b]).returncode == 0
        outcome = _cleanup_owned_image(id_a, run_a)
        assert outcome in ("removed", "absent", "preserved-conflict",
                           "preserved-unverified"), outcome
        b_after = _inspect_image(id_b)
        assert b_after.state is InspectState.FOUND, (
            "replacement image B was deleted by cleanup")
        assert b_after.image_id == id_b
        if outcome == "removed":
            assert _inspect_image(id_a).state is InspectState.NOT_FOUND
    finally:
        _cleanup_owned_image(id_b, run_b)
        # A is test-owned too: remove leftovers strictly by recorded ID.
        left = _inspect_image(id_a)
        if left.state is InspectState.FOUND \
                and left.labels.get(RUN_LABEL) == run_a:
            _run(["docker", "rmi", id_a], timeout=120)


def test_cleanup_refuses_multiply_tagged_image(tmp_path):
    _tag, run_id, image_id = _build_labeled_image(
        tmp_path, "multi", {RUN_LABEL: uuid.uuid4().hex})
    extra_tag, _extra_run = _unique_sentinel_tag()
    try:
        assert _run(["docker", "tag", image_id, extra_tag]).returncode == 0
        outcome = _cleanup_owned_image(image_id, run_id)
        assert outcome == "preserved-conflict", outcome  # no force used
        still = _inspect_image(image_id)
        assert still.state is InspectState.FOUND
        assert still.image_id == image_id
        # Teardown of our own fixture: drop the extra tag, then the
        # verified ID cleanup succeeds.
        assert _run(["docker", "rmi", extra_tag]).returncode == 0
        assert _cleanup_owned_image(image_id, run_id) == "removed"
    finally:
        left = _inspect_image(image_id)
        if left.state is InspectState.FOUND \
                and left.labels.get(RUN_LABEL) == run_id:
            _run(["docker", "rmi", extra_tag], timeout=120)
            _cleanup_owned_image(image_id, run_id)


def test_sequential_matcher_invocations_use_distinct_tags(tmp_path):
    first = _run_matcher_build(tmp_path / "one")
    second = _run_matcher_build(tmp_path / "two")
    assert first[0] != second[0] and first[1] != second[1]
    _assert_matcher_listing(first[2])
    _assert_matcher_listing(second[2])
    assert _inspect_image(first[0]).state is InspectState.NOT_FOUND


def test_concurrent_matcher_invocations_do_not_interfere(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def work(name: str):
        return _run_matcher_build(tmp_path / name)

    (tmp_path / "c1").mkdir()
    (tmp_path / "c2").mkdir()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, ["c1", "c2"]))
    assert results[0][0] != results[1][0]
    assert results[0][1] != results[1][1]
    for _tag, _run_id, listed in results:
        _assert_matcher_listing(listed)
        assert _inspect_image(_tag).state is InspectState.NOT_FOUND


def _non_loopback_ipv4() -> str | None:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return None
    for info in infos:
        ip = str(info[4][0])
        if not ip.startswith(("127.", "169.254.", "172.16.", "172.17.",
                              "172.18.", "172.19.", "172.2", "172.30.",
                              "172.31.")):
            return ip
    return None


def test_stack_loopback_binding_flow_and_negative_probe(tmp_path):
    backend_port, frontend_port = _free_port(), _free_port()
    project = f"tf-m5-it-{os.getpid()}"
    override = tmp_path / "override.yml"
    override.write_text(
        "services:\n"
        "  backend:\n"
        "    environment:\n"
        f"      TRUSTFLOW_CORS_ORIGINS: '[\"http://127.0.0.1:{frontend_port}\","
        f"\"http://localhost:{frontend_port}\"]'\n"
        "    ports: !override\n"
        f"      - \"127.0.0.1:{backend_port}:8000\"\n"
        "  frontend:\n"
        "    build:\n"
        "      args:\n"
        f"        NEXT_PUBLIC_API_URL: \"http://127.0.0.1:{backend_port}\"\n"
        "    ports: !override\n"
        f"      - \"127.0.0.1:{frontend_port}:3000\"\n",
        encoding="utf-8")
    base = ["docker", "compose", "-p", project,
            "-f", str(ROOT / "docker-compose.yml"), "-f", str(override)]
    try:
        up = _run([*base, "up", "-d", "--build"])
        assert up.returncode == 0, up.stderr[-2000:]
        health = f"http://127.0.0.1:{backend_port}/health"
        deadline = time.time() + 120
        status, body = 0, {}
        while time.time() < deadline:
            try:
                status, body = _http("GET", health, timeout=5)
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(2)
        assert status == 200 and body.get("llm_mode") == "fake", body
        assert body.get("persistence") == "sqlite"

        for service, port in (("backend", backend_port), ("frontend", frontend_port)):
            cid = _run([*base, "ps", "-q", service]).stdout.strip()
            assert cid, service
            ports = json.loads(_run(
                ["docker", "inspect", "-f",
                 "{{json .NetworkSettings.Ports}}", cid]).stdout)
            bindings = ports[f"{8000 if service == 'backend' else 3000}/tcp"]
            assert bindings and all(
                b.get("HostIp") == "127.0.0.1" and
                int(b.get("HostPort", 0)) == port for b in bindings), bindings

        # Browser-path flow over loopback: create -> approve -> completed.
        origin = {"Origin": f"http://127.0.0.1:{frontend_port}"}
        status, _page = _http("GET", f"http://127.0.0.1:{frontend_port}/")
        assert status == 200
        status, run = _http("POST", f"http://127.0.0.1:{backend_port}/workflows",
                            {"title": "Transfer 5000",
                             "description": "transfer 5000 to bob"}, origin)
        assert run.get("status") == "AWAITING_APPROVAL", run
        run_id, plan_hash = run["run_id"], run["plan"]["plan_hash"]
        status, done = _http(
            "POST", f"http://127.0.0.1:{backend_port}/workflows/{run_id}/approve",
            {"actor": "mgr", "plan_hash": plan_hash},
            {**origin, "X-Approval-Token": TOKEN})
        assert status == 200 and done.get("status") == "COMPLETED", done
        txs = [r["data"]["tx_id"] for r in done["step_results"]
               if r.get("data", {}).get("tx_id")]
        assert len(txs) == 1

        # Restart: durable, single transfer, replay rejected.
        _run([*base, "restart", "backend"])
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if _http("GET", health, timeout=5)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(2)
        status, after = _http("GET", f"http://127.0.0.1:{backend_port}/workflows/{run_id}")
        assert after.get("status") == "COMPLETED"
        assert [r["data"]["tx_id"] for r in after["step_results"]
                if r.get("data", {}).get("tx_id")] == txs
        status, _ = _http(
            "POST", f"http://127.0.0.1:{backend_port}/workflows/{run_id}/approve",
            {"actor": "mgr", "plan_hash": plan_hash}, {"X-Approval-Token": TOKEN})
        assert status == 409

        # Negative: the published services must not answer on Ethernet.
        lan_ip = _non_loopback_ipv4()
        if lan_ip is None:
            pytest.skip("no non-loopback host IPv4 available for negative probe")
        for port in (backend_port, frontend_port):
            with pytest.raises(OSError):
                _http("GET", f"http://{lan_ip}:{port}/health"
                      if port == backend_port else f"http://{lan_ip}:{port}/",
                      timeout=3)
    finally:
        _run([*base, "down", "-v"], timeout=300)
