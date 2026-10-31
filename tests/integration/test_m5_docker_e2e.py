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


# --- Per-invocation image ownership (P2-05) ---------------------------
# The historical matcher test built into the shared fixed tag
# `trustflow-m5-matcher-test:latest` and removed it unconditionally,
# destroying a pre-existing image owned by someone else. Now every
# invocation builds into a UUID tag carrying an ownership label; the
# created image ID is recorded, and cleanup re-verifies label + ID
# before removing exactly that tag (never `-f`, never a shared tag).

RUN_LABEL = "trustflow.test-run"
LEGACY_FIXED_TAG = "trustflow-m5-matcher-test:latest"  # never built now


def _unique_image_tag() -> tuple[str, str]:
    run_id = uuid.uuid4().hex
    return f"trustflow-m5-matcher-test:{run_id}", run_id


def _owned_image_ok(labels: dict | None, image_id: str | None,
                    run_id: str, expected_id: str | None) -> bool:
    return bool(image_id) and image_id == expected_id and bool(labels) \
        and labels.get(RUN_LABEL) == run_id


def _image_inspect(ref: str) -> dict | None:
    proc = _run(["docker", "image", "inspect", ref], timeout=120)
    if proc.returncode != 0:
        return None
    items = json.loads(proc.stdout)
    return items[0] if items else None


def _cleanup_owned_image(tag: str, run_id: str, expected_id: str | None) -> str:
    """Remove only the verified image this invocation created.

    Returns "removed" / "absent" / "preserved-unverified" /
    "cleanup-failed". Anything unverifiable is preserved and made
    visible via a warning instead of risking foreign deletion.
    """
    current = _image_inspect(tag)
    if current is None:
        return "absent"
    labels = (current.get("Config") or {}).get("Labels") or {}
    if not _owned_image_ok(labels, current.get("Id"), run_id, expected_id):
        warnings.warn(f"P2-05: preserving unverified image {tag} "
                      f"(id={current.get('Id')}, labels={labels})", stacklevel=2)
        return "preserved-unverified"
    proc = _run(["docker", "rmi", tag], timeout=120)  # no -f: exact tag
    if proc.returncode != 0:
        warnings.warn(f"P2-05: cleanup of owned image {tag} failed: "
                      f"{proc.stderr.strip()}", stacklevel=2)
        return "cleanup-failed"
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
    tag = run_id = ""
    for _attempt in range(5):  # a fresh UUID tag must not pre-exist
        tag, run_id = _unique_image_tag()
        if _image_inspect(tag) is None:
            break
    else:
        raise AssertionError("could not allocate a non-existing image tag")
    image_id: str | None = None
    try:
        build = _run(["docker", "build", "--label", f"{RUN_LABEL}={run_id}",
                      "-t", tag, str(ctx)])
        assert build.returncode == 0, build.stderr[-2000:]
        created = _image_inspect(tag)
        assert created is not None, f"built image {tag} not inspectable"
        image_id = created.get("Id")
        labels = (created.get("Config") or {}).get("Labels") or {}
        assert labels.get(RUN_LABEL) == run_id, labels
        out = _run(["docker", "run", "--rm", "--entrypoint", "cat",
                    tag, "/listing.txt"])
        assert out.returncode == 0, out.stderr
        return tag, run_id, set(out.stdout.split())
    finally:
        outcome = _cleanup_owned_image(tag, run_id, image_id)
        assert outcome in ("removed", "absent"), outcome


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


def test_matcher_preserves_preexisting_fixed_tag(tmp_path):
    """P2-05 regression: the matcher run must never touch the legacy tag.

    A sentinel is placed under the historical fixed tag only when that
    tag is initially absent (created by this invocation, removed by
    this invocation afterwards). A pre-existing tag is preserved and
    only its identity is validated.
    """
    sentinel_ctx = tmp_path / "sentinel"
    sentinel_ctx.mkdir()
    (sentinel_ctx / "Dockerfile").write_text(
        'FROM node:22-alpine\nLABEL trustflow.sentinel="m5-p2-05"\n',
        encoding="utf-8")
    pre = _image_inspect(LEGACY_FIXED_TAG)
    created_here = False
    if pre is None:
        build = _run(["docker", "build", "-t", LEGACY_FIXED_TAG,
                      str(sentinel_ctx)])
        assert build.returncode == 0, build.stderr[-2000:]
        created_here = True
        pre = _image_inspect(LEGACY_FIXED_TAG)
    assert pre is not None and pre.get("Id")
    sentinel_id = pre["Id"]
    try:
        tag, _run_id, listed = _run_matcher_build(tmp_path)
        assert tag != LEGACY_FIXED_TAG
        _assert_matcher_listing(listed)
        after = _image_inspect(LEGACY_FIXED_TAG)
        assert after is not None and after.get("Id") == sentinel_id, (
            "pre-existing fixed-tag image was overwritten or deleted")
    finally:
        if created_here:
            # Only this invocation's own sentinel is cleanup-eligible;
            # a genuinely pre-existing image would be preserved.
            current = _image_inspect(LEGACY_FIXED_TAG)
            if current is not None and current.get("Id") == sentinel_id:
                proc = _run(["docker", "rmi", LEGACY_FIXED_TAG], timeout=120)
                assert proc.returncode == 0, proc.stderr


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
