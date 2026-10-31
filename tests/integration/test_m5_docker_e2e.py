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
import re
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


# --- Untagged builds + immutable-ID lifecycle (P2-05 R4) -------------
# Structural fix, not another tag workaround: temporary images are
# built WITHOUT any name (--iidfile captures the immutable image ID),
# so no tag is ever allocated, written, transferred or deleted by the
# tests. Inspection can establish FOUND only from a successful, valid
# response; the Docker CLI cannot distinguish HTTP 404 from HTTP 500
# absence-shaped failures, so every unsuccessful inspection is ERROR
# and never authorizes a mutation. Cleanup deletes only recorded,
# label-verified image IDs, without force.

RUN_LABEL = "trustflow.test-run"
NONCE_LABEL = "trustflow.build-nonce"
SENTINEL_LABEL = "trustflow.sentinel"
LEGACY_FIXED_TAG = "trustflow-m5-matcher-test:latest"  # read-only only


class InspectState(Enum):
    FOUND = "found"
    # Reserved: only authoritative structured evidence (e.g. an
    # Engine API 404) could establish this. The Docker CLI path in
    # this module never returns it — unsuccessful CLI inspections
    # are indistinguishable from failures and yield ERROR.
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class InspectResult:
    state: InspectState
    image_id: str | None = None
    labels: dict = field(default_factory=dict)
    detail: str = ""


_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _inspect_image(ref: str) -> InspectResult:
    """Image inspection that never guesses absence.

    FOUND requires a successful response with strictly valid
    metadata. Every unsuccessful or ambiguous result is ERROR: the
    Docker CLI emits byte-identical rc/stdout/stderr for Engine
    HTTP 404 (genuinely absent) and HTTP 500 (inspection failed),
    so nonzero CLI output cannot prove absence and must never
    authorize build, overwrite or deletion.
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
        item = items[0]
        image_id = item.get("Id")
        config = item.get("Config")
        if not isinstance(image_id, str) \
                or not _IMAGE_ID_RE.fullmatch(image_id) \
                or not isinstance(config, dict):
            return InspectResult(InspectState.ERROR,
                                 detail=f"invalid inspect metadata for {ref}")
        labels = config.get("Labels")
        if labels is None:
            labels = {}
        if not isinstance(labels, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in labels.items()):
            return InspectResult(InspectState.ERROR,
                                 detail=f"invalid inspect labels for {ref}")
        return InspectResult(InspectState.FOUND, image_id=image_id,
                             labels=dict(labels))
    # Nonzero exit: absence cannot be distinguished from failure
    # through CLI text (HTTP 404 vs 500 are byte-identical here).
    return InspectResult(
        InspectState.ERROR,
        detail=f"inspect rc={proc.returncode} for {ref}: "
               f"{(proc.stderr or '').strip()[:300]}")


def _owned_image_ok(labels: dict | None, image_id: str | None,
                    run_id: str, expected_id: str | None) -> bool:
    return bool(image_id) and image_id == expected_id and bool(labels) \
        and labels.get(RUN_LABEL) == run_id


def _fail_if_error(res: InspectResult, what: str) -> None:
    """Fail closed when a safety-relevant inspection is inconclusive."""
    if res.state is InspectState.ERROR:
        raise AssertionError(f"{what}: inspection ERROR: {res.detail}")


class CleanupFailure(AssertionError):
    """One mandatory cleanup failure, with full resource identity."""

    def __init__(self, operation: str, image_id: str | None,
                 run_id: str, command: str, reason: str,
                 preserved: bool) -> None:
        super().__init__(
            f"{operation} failed for image_id={image_id} run={run_id} "
            f"command={command!r} reason={reason} preserved={preserved}")
        self.operation = operation
        self.image_id = image_id
        self.run_id = run_id
        self.command = command
        self.reason = reason
        self.preserved = preserved


class ImageLifecycle:
    """Owns every untagged temporary image of one test invocation.

    Resources are registered the moment their immutable ID becomes
    known. Exit always attempts verified ID-only cleanup; primary
    and cleanup failures are both preserved (ExceptionGroup with
    the primary as cause — neither is silently replaced).
    """

    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex
        self._registered: list[str] = []
        self.cleanup_failures: list[CleanupFailure] = []

    def __enter__(self) -> "ImageLifecycle":
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        failures = self._cleanup_all()
        if exc is not None:
            if failures:
                raise ExceptionGroup(
                    "primary failure plus image cleanup failures",
                    failures) from exc
            return False
        if failures:
            raise ExceptionGroup("image cleanup failed", failures)
        return False

    def build_image(self, ctx: Path, name: str,
                    extra_labels: dict[str, str] | None = None) -> str:
        """Build untagged via --iidfile; return the immutable ID."""
        nonce = uuid.uuid4().hex
        iidfile = ctx.parent / f".iid-{self.run_id}-{name}"
        labels = {RUN_LABEL: self.run_id, NONCE_LABEL: nonce,
                  **(extra_labels or {})}
        argv = ["docker", "build", "--iidfile", str(iidfile)]
        for key, value in labels.items():
            argv += ["--label", f"{key}={value}"]
        argv.append(str(ctx))  # no -t: the image stays unnamed
        build = _run(argv)
        if build.returncode != 0:
            raise AssertionError(
                f"untagged build failed for run={self.run_id} "
                f"context={ctx}: {(build.stderr or '')[-500:]}")
        try:
            image_id = iidfile.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AssertionError(
                f"build for run={self.run_id} context={ctx} produced "
                f"no readable iidfile at {iidfile} ({exc}); an "
                f"untagged leftover cannot be identified and is not "
                f"deleted") from exc
        if not _IMAGE_ID_RE.fullmatch(image_id):
            raise AssertionError(
                f"invalid iidfile content {image_id!r} for "
                f"run={self.run_id} context={ctx}; image identity "
                f"unknown, nothing deleted")
        self._registered.append(image_id)  # tracked from first identity
        res = _inspect_image(image_id)
        if res.state is not InspectState.FOUND:
            raise AssertionError(
                f"post-build inspection {res.state.value} for "
                f"image_id={image_id} run={self.run_id}: {res.detail}; "
                f"ownership unknown, image preserved")
        if res.image_id != image_id \
                or res.labels.get(RUN_LABEL) != self.run_id:
            raise AssertionError(
                f"ownership verification failed for "
                f"image_id={image_id} run={self.run_id}: "
                f"inspected id={res.image_id} labels={res.labels}; "
                f"image preserved")
        return image_id

    def _cleanup_all(self) -> list[CleanupFailure]:
        failures: list[CleanupFailure] = []
        for image_id in reversed(self._registered):
            command = f"docker rmi {image_id}"
            try:
                outcome = _cleanup_owned_image(image_id, self.run_id)
            except BaseException as exc:  # e.g. removal timeout
                failures.append(CleanupFailure(
                    "cleanup", image_id, self.run_id, command,
                    f"{type(exc).__name__}: {exc}", preserved=True))
                continue
            if outcome not in ("removed", "absent"):
                failures.append(CleanupFailure(
                    "cleanup", image_id, self.run_id, command,
                    f"outcome={outcome}", preserved=True))
        self.cleanup_failures = failures
        return failures


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


def _matcher_context(tmp_path: Path) -> Path:
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
    return ctx


def _run_matcher_build(tmp_path: Path) -> tuple[str, set[str]]:
    """Untagged matcher build; returns (image_id, listed paths).

    The lifecycle registers the ID before any use and removes it by
    ID on exit; failures aggregate instead of masking each other.
    """
    with ImageLifecycle() as lifecycle:
        image_id = lifecycle.build_image(_matcher_context(tmp_path),
                                         "matcher")
        out = _run(["docker", "run", "--rm", "--entrypoint", "cat",
                    image_id, "/listing.txt"])
        assert out.returncode == 0, out.stderr
        return image_id, set(out.stdout.split())


def _tiny_context(tmp_path: Path, name: str) -> Path:
    ctx = tmp_path / name
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM node:22-alpine\n",
                                    encoding="utf-8")
    return ctx


def _raw_inspect(ref: str) -> dict:
    # Generic inspect: valid for images and containers alike.
    proc = _run(["docker", "inspect", ref], timeout=120)
    assert proc.returncode == 0, proc.stderr
    items = json.loads(proc.stdout)
    assert isinstance(items, list) and len(items) == 1
    return items[0]


def _image_inventory() -> dict[str, dict]:
    """Full identity mapping: ID -> tags/digests/labels (no display
    fields that change over time)."""
    proc = _run(["docker", "image", "ls", "-q", "--no-trunc"], timeout=120)
    assert proc.returncode == 0, proc.stderr
    inventory: dict[str, dict] = {}
    for image_id in sorted(set(proc.stdout.split())):
        item = _raw_inspect(image_id)
        inventory[image_id] = {
            "tags": sorted(item.get("RepoTags") or []),
            "digests": sorted(item.get("RepoDigests") or []),
            "labels": (item.get("Config") or {}).get("Labels") or {},
        }
    return inventory


def _assert_matcher_listing(listed: set[str]) -> None:
    for artifact in ("node_modules", ".next", "NESTED_NODE", "NESTED_NEXT",
                     "npm-debug.log.1"):
        assert not any(artifact in p for p in listed), (artifact, listed)
    for required in ("/ctx/package.json", "/ctx/package-lock.json",
                     "/ctx/app/page.tsx", "/ctx/lib/api.ts"):
        assert required in listed, listed


def test_real_docker_matcher_frontend_context(tmp_path):
    _image_id, listed = _run_matcher_build(tmp_path)
    _assert_matcher_listing(listed)


def test_untagged_build_lifecycle(tmp_path):
    """Real iidfile build: unnamed, labelled, removed by ID on exit."""
    with ImageLifecycle() as lifecycle:
        image_id = lifecycle.build_image(
            _tiny_context(tmp_path, "tiny"), "tiny")
        raw = _raw_inspect(image_id)
        assert not raw.get("RepoTags")  # built without any name
        labels = (raw.get("Config") or {}).get("Labels") or {}
        assert labels.get(RUN_LABEL) == lifecycle.run_id
        assert labels.get(NONCE_LABEL)
    assert _inspect_image(image_id).state is not InspectState.FOUND


def test_matcher_preserves_unrelated_images(tmp_path):
    """P2-05 R2 sentinel design: tests never write the legacy tag.

    A pre-existing legacy image is only observed read-only through
    the successful structured image inventory (never written); the
    sentinel is an untagged lifecycle image cleaned up by ID.
    """
    before = _image_inventory()
    legacy_id = next(
        (iid for iid, meta in before.items()
         if LEGACY_FIXED_TAG in meta["tags"]), None)
    with ImageLifecycle() as lifecycle:
        s_id = lifecycle.build_image(
            _tiny_context(tmp_path, "sentinel"), "sentinel",
            {SENTINEL_LABEL: "m5-p2-05"})
        _image_id, listed = _run_matcher_build(tmp_path)
        _assert_matcher_listing(listed)
        after_s = _inspect_image(s_id)
        assert after_s.state is InspectState.FOUND
        assert after_s.image_id == s_id
        assert after_s.labels.get(SENTINEL_LABEL) == "m5-p2-05"
        after = _image_inventory()
        for image_id, meta in before.items():
            assert after.get(image_id) == meta, image_id
        if legacy_id is None:
            assert not any(LEGACY_FIXED_TAG in meta["tags"]
                           for meta in after.values())


def test_inspect_states_against_real_docker(tmp_path):
    # CLI nonzero output cannot prove absence (404 vs 500 are
    # byte-identical): an unknown ref is conservatively ERROR.
    missing = f"trustflow-absent:{uuid.uuid4().hex}"
    assert _inspect_image(missing).state is InspectState.ERROR
    with ImageLifecycle() as lifecycle:
        image_id = lifecycle.build_image(
            _tiny_context(tmp_path, "found"), "found")
        found = _inspect_image(image_id)
        assert found.state is InspectState.FOUND
        assert found.image_id == image_id
        assert found.labels.get(RUN_LABEL) == lifecycle.run_id


def test_preexisting_image_mapping_preserved(tmp_path):
    """No pre-existing ID/tag/digest/label mapping may change."""
    before = _image_inventory()
    _image_id, listed = _run_matcher_build(tmp_path)
    _assert_matcher_listing(listed)
    after = _image_inventory()
    for image_id, meta in before.items():
        assert after.get(image_id) == meta, image_id


def test_cleanup_refused_by_attached_container(tmp_path):
    """Real refusal: an attached container blocks ID deletion (no
    force); after removing our own container, cleanup succeeds."""
    with ImageLifecycle() as lifecycle:
        image_id = lifecycle.build_image(
            _tiny_context(tmp_path, "held"), "held")
        created = _run(["docker", "create", image_id], timeout=120)
        assert created.returncode == 0, created.stderr
        container_id = created.stdout.strip()
        try:
            outcome = _cleanup_owned_image(image_id, lifecycle.run_id)
            assert outcome == "preserved-conflict", outcome
            assert _inspect_image(image_id).state is InspectState.FOUND
        finally:
            removed = _run(["docker", "rm", container_id], timeout=120)
            assert removed.returncode == 0, removed.stderr
    assert _inspect_image(image_id).state is not InspectState.FOUND


def test_sequential_matcher_invocations_use_distinct_ids(tmp_path):
    first_id, first_listed = _run_matcher_build(tmp_path / "one")
    second_id, second_listed = _run_matcher_build(tmp_path / "two")
    assert first_id != second_id  # distinct nonce labels -> identities
    _assert_matcher_listing(first_listed)
    _assert_matcher_listing(second_listed)
    assert _inspect_image(first_id).state is not InspectState.FOUND
    assert _inspect_image(second_id).state is not InspectState.FOUND


def test_concurrent_matcher_invocations_do_not_interfere(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def work(name: str):
        return _run_matcher_build(tmp_path / name)

    (tmp_path / "c1").mkdir()
    (tmp_path / "c2").mkdir()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, ["c1", "c2"]))
    assert results[0][0] != results[1][0]
    for _image_id, listed in results:
        _assert_matcher_listing(listed)
        assert _inspect_image(_image_id).state is not InspectState.FOUND


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


PROJECT_LABEL = "com.docker.compose.project"


class NamespaceConflict(AssertionError):
    """The proposed Compose namespace is already occupied."""


def _labelled_ids(argv: list[str]) -> list[str]:
    proc = _run(argv, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.split() if line.strip()]


def _compose_preflight(project: str) -> None:
    """Fail closed BEFORE any mutation if the namespace is occupied.

    UUID uniqueness alone is never the ownership proof: any existing
    container/image/network/volume carrying the Compose project
    label for the proposed namespace rejects it outright, and no
    teardown is ever aimed at it.
    """
    occupied = {
        "containers": _labelled_ids(
            ["docker", "ps", "-a", "-q", "--filter",
             f"label={PROJECT_LABEL}={project}"]),
        "images": _labelled_ids(
            ["docker", "image", "ls", "-q", "--filter",
             f"label={PROJECT_LABEL}={project}"]),
        "networks": _labelled_ids(
            ["docker", "network", "ls", "-q", "--filter",
             f"label={PROJECT_LABEL}={project}"]),
        "volumes": _labelled_ids(
            ["docker", "volume", "ls", "-q", "--filter",
             f"label={PROJECT_LABEL}={project}"]),
    }
    found = {k: v for k, v in occupied.items() if v}
    if found:
        raise NamespaceConflict(
            f"compose namespace {project} already occupied: {found}; "
            f"refusing adoption or teardown of unowned resources")


def _raise_stack_result(primary: BaseException | None,
                        failures: list[BaseException]) -> None:
    """Aggregate primary + cleanup failures; neither is ever lost."""
    if primary is not None and failures:
        raise ExceptionGroup(
            "stack failure plus cleanup failures", failures) from primary
    if primary is not None:
        raise primary
    if failures:
        raise ExceptionGroup("stack cleanup failed", failures)


class _StackRun:
    """One UUID-owned Compose stack invocation (R5 F1/F2/F3).

    Ownership = empty namespace at preflight + per-invocation run
    label present on containers/networks/volumes/build images +
    image IDs recorded and verified after creation. Teardown never
    runs against an unverified namespace; image cleanup is by
    recorded immutable ID only (no --rmi, no force, no prune).
    """

    def __init__(self, tmp_path: Path, *, project: str | None = None,
                 run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex
        self.project = project or f"tf-m5-it-{self.run_id}"
        self.backend_port = _free_port()
        self.frontend_port = _free_port()
        label = f"{RUN_LABEL}={self.run_id}"
        override = tmp_path / f"override-{self.run_id}.yml"
        override.write_text(
            "services:\n"
            "  backend:\n"
            "    build:\n"
            "      labels:\n"
            f"        - \"{label}\"\n"
            "    labels:\n"
            f"      - \"{label}\"\n"
            "    environment:\n"
            f"      TRUSTFLOW_CORS_ORIGINS: "
            f"'[\"http://127.0.0.1:{self.frontend_port}\","
            f"\"http://localhost:{self.frontend_port}\"]'\n"
            "    ports: !override\n"
            f"      - \"127.0.0.1:{self.backend_port}:8000\"\n"
            "  frontend:\n"
            "    build:\n"
            "      args:\n"
            f"        NEXT_PUBLIC_API_URL: "
            f"\"http://127.0.0.1:{self.backend_port}\"\n"
            "      labels:\n"
            f"        - \"{label}\"\n"
            "    labels:\n"
            f"      - \"{label}\"\n"
            "    ports: !override\n"
            f"      - \"127.0.0.1:{self.frontend_port}:3000\"\n"
            "networks:\n"
            "  default:\n"
            "    labels:\n"
            f"      - \"{label}\"\n"
            "volumes:\n"
            "  tf-data:\n"
            "    labels:\n"
            f"      - \"{label}\"\n",
            encoding="utf-8")
        self.base = ["docker", "compose", "-p", self.project,
                     "-f", str(ROOT / "docker-compose.yml"),
                     "-f", str(override)]
        self.preflight_inventory: dict[str, dict] = {}
        self.image_ids: dict[str, str] = {}
        self.ownership_verified = False

    def up(self) -> None:
        _compose_preflight(self.project)  # before ANY mutation
        self.preflight_inventory = _image_inventory()
        up = _run([*self.base, "up", "-d", "--build"])
        assert up.returncode == 0, up.stderr[-2000:]
        self._wait_health()
        self._verify_ownership()

    def _wait_health(self) -> None:
        health = f"http://127.0.0.1:{self.backend_port}/health"
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

    def _container_id(self, service: str) -> str:
        cid = _run([*self.base, "ps", "-q", service]).stdout.strip()
        assert cid, service
        return cid

    def _verify_ownership(self) -> None:
        for service in ("backend", "frontend"):
            cid = self._container_id(service)
            raw = _raw_inspect(cid)
            labels = (raw.get("Config") or {}).get("Labels") or {}
            assert labels.get(PROJECT_LABEL) == self.project, labels
            assert labels.get(RUN_LABEL) == self.run_id, labels
            image_id = raw.get("Image")
            assert isinstance(image_id, str) and image_id
            assert image_id not in self.preflight_inventory
            self.image_ids[service] = image_id
        for image_id in self.image_ids.values():
            res = _inspect_image(image_id)
            assert res.state is InspectState.FOUND, res.detail
            assert res.labels.get(RUN_LABEL) == self.run_id, res.labels
            assert res.labels.get(PROJECT_LABEL) == self.project
        self.ownership_verified = True


    def exercise(self) -> None:
        backend_port, frontend_port = self.backend_port, self.frontend_port
        base, health = self.base, \
            f"http://127.0.0.1:{self.backend_port}/health"
        for service, port in (("backend", backend_port),
                              ("frontend", frontend_port)):
            cid = self._container_id(service)
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
        status = 0
        while time.time() < deadline:
            try:
                if _http("GET", health, timeout=5)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(2)
        status, after = _http(
            "GET", f"http://127.0.0.1:{backend_port}/workflows/{run_id}")
        assert after.get("status") == "COMPLETED"
        assert [r["data"]["tx_id"] for r in after["step_results"]
                if r.get("data", {}).get("tx_id")] == txs
        status, _ = _http(
            "POST", f"http://127.0.0.1:{backend_port}/workflows/{run_id}/approve",
            {"actor": "mgr", "plan_hash": plan_hash},
            {"X-Approval-Token": TOKEN})
        assert status == 409

        # Negative: the published services must not answer on Ethernet.
        lan_ip = _non_loopback_ipv4()
        if lan_ip is None:
            pytest.skip("no non-loopback host IPv4 available")
        for port in (backend_port, frontend_port):
            with pytest.raises(OSError):
                _http("GET", f"http://{lan_ip}:{port}/health"
                      if port == backend_port else f"http://{lan_ip}:{port}/",
                      timeout=3)

    def _collect_owned(self) -> dict[str, list[str]]:
        def ids(argv: list[str]) -> list[str]:
            try:
                proc = _run(argv, timeout=120)
            except BaseException:
                return []
            if proc.returncode != 0:
                return []
            return [x for x in proc.stdout.split() if x.strip()]

        label = f"label={RUN_LABEL}={self.run_id}"
        return {
            "containers": ids(["docker", "ps", "-a", "-q",
                               "--filter", label]),
            "networks": ids(["docker", "network", "ls", "-q",
                             "--filter", label]),
            "volumes": ids(["docker", "volume", "ls", "-q",
                            "--filter", label]),
            "images": ids(["docker", "image", "ls", "-q",
                           "--filter", label]),
        }

    def teardown(self) -> list[BaseException]:
        """Verified teardown; returns failures, never raises raw.

        No destructive command runs against a namespace this
        invocation never owned. Image cleanup is by recorded ID;
        postconditions are verified, not inferred from exit codes.
        """
        failures: list[BaseException] = []

        def fail(op: str, image_id: str | None, cmd: str, reason: str):
            failures.append(CleanupFailure(
                op, image_id, self.run_id, cmd, reason, preserved=True))

        owned = self._collect_owned()
        if not self.ownership_verified \
                and not any(owned.values()):
            return failures  # never owned: nothing may be destroyed
        down_cmd = " ".join([*self.base, "down", "-v"])
        try:
            down = _run([*self.base, "down", "-v"], timeout=300)
            if down.returncode != 0:
                fail("compose-down", None, down_cmd,
                     f"rc={down.returncode}: "
                     f"{(down.stderr or '')[-300:]}")
        except BaseException as exc:  # incl. TimeoutExpired (F3)
            fail("compose-down", None, down_cmd,
                 f"{type(exc).__name__}: {exc}")
        # Image cleanup by recorded ID; images of a partially built
        # stack are also collected via the run label and positively
        # label-verified before any deletion is attempted.
        targets: dict[str, str] = dict(self.image_ids)
        for ref in owned["images"]:
            res = _inspect_image(ref)
            if res.state is InspectState.FOUND and res.image_id \
                    and res.labels.get(RUN_LABEL) == self.run_id:
                targets.setdefault(res.image_id, res.image_id)
        for image_id in dict.fromkeys(targets.values()):
            service = image_id
            try:
                outcome = _cleanup_owned_image(image_id, self.run_id)
            except BaseException as exc:
                fail(f"image-cleanup:{service}", image_id,
                     f"docker rmi {image_id}",
                     f"{type(exc).__name__}: {exc}")
                continue
            if outcome not in ("removed", "absent"):
                fail(f"image-cleanup:{service}", image_id,
                     f"docker rmi {image_id}", f"outcome={outcome}")
        try:
            remaining = self._collect_owned()
            for kind in ("containers", "networks", "volumes", "images"):
                if remaining[kind]:
                    fail(f"postcondition:{kind}", None, down_cmd,
                         f"still present: {remaining[kind]}")
            for image_id in self.image_ids.values():
                if _inspect_image(image_id).state is InspectState.FOUND:
                    fail("postcondition:image", image_id,
                         f"docker rmi {image_id}",
                         "image still present after cleanup")
            after = _image_inventory()
            for old_id, meta in self.preflight_inventory.items():
                if after.get(old_id) != meta:
                    fail("postcondition:preexisting", old_id,
                         "inventory-compare",
                         "pre-existing image mapping changed")
        except BaseException as exc:
            fail("postcondition", None, down_cmd,
                 f"{type(exc).__name__}: {exc}")
        return failures


def _run_stack_test(tmp_path: Path) -> None:
    run = _StackRun(tmp_path)
    primary: BaseException | None = None
    try:
        run.up()
        run.exercise()
    except BaseException as exc:
        primary = exc
    failures: list[BaseException] = []
    if not isinstance(primary, NamespaceConflict):
        failures = run.teardown()
    _raise_stack_result(primary, failures)


def test_stack_loopback_binding_flow_and_negative_probe(tmp_path):
    _run_stack_test(tmp_path)


def test_stack_rejects_preexisting_namespace(tmp_path):
    """F1 regression: an occupied UUID namespace is never adopted.

    The victim image/network/volume are created by THIS test (own
    invocation resources) and removed by recorded identity at the
    end; the candidate session must refuse before any mutation.
    """
    victim = f"tf-m5-victim-{uuid.uuid4().hex[:16]}"
    net_name, vol_name = f"{victim}_default", f"{victim}_tf-data"
    with ImageLifecycle() as lifecycle:
        image_id = lifecycle.build_image(
            _tiny_context(tmp_path, "victim"), "victim",
            {PROJECT_LABEL: victim, "trustflow.review": "victim"})
        proc_net = _run(["docker", "network", "create", "--label",
                         f"{PROJECT_LABEL}={victim}", net_name])
        assert proc_net.returncode == 0, proc_net.stderr
        proc_vol = _run(["docker", "volume", "create", "--label",
                         f"{PROJECT_LABEL}={victim}", vol_name])
        assert proc_vol.returncode == 0, proc_vol.stderr
        try:
            candidate = _StackRun(tmp_path, project=victim)
            with pytest.raises(NamespaceConflict):
                candidate.up()
            # Nothing pre-existing was mutated or torn down.
            assert _inspect_image(image_id).state is InspectState.FOUND
            assert _labelled_ids(
                ["docker", "network", "ls", "-q", "--filter",
                 f"label={PROJECT_LABEL}={victim}"])
            assert _labelled_ids(
                ["docker", "volume", "ls", "-q", "--filter",
                 f"label={PROJECT_LABEL}={victim}"])
        finally:
            _run(["docker", "network", "rm", net_name], timeout=120)
            _run(["docker", "volume", "rm", vol_name], timeout=120)
    assert _inspect_image(image_id).state is not InspectState.FOUND


def test_stack_shared_image_refusal_fails_cleanup(tmp_path):
    """F2 regression: a shared owned image blocks mandatory cleanup.

    The unrelated stopped container is created by THIS test from the
    recorded backend image ID; teardown must preserve both, report
    FAILED cleanup (never PASS), and controlled cleanup afterwards
    restores the inventory.
    """
    run = _StackRun(tmp_path)
    container_id: str | None = None
    completed = False
    try:
        run.up()
        backend_id = run.image_ids["backend"]
        created = _run(["docker", "create", backend_id], timeout=120)
        assert created.returncode == 0, created.stderr
        container_id = created.stdout.strip()
        run.exercise()
        failures = run.teardown()
        assert failures, "mandatory cleanup must FAIL on shared image"
        assert any(backend_id in str(f) for f in failures), failures
        assert _inspect_image(backend_id).state is InspectState.FOUND
        alive = _run(["docker", "inspect", "-f", "{{.Id}}",
                      container_id], timeout=120)
        assert alive.returncode == 0  # unrelated container intact
        completed = True
    finally:
        if container_id is not None:
            _run(["docker", "rm", container_id], timeout=120)
        if not completed:
            run.teardown()  # own resources only; outcome already failing
    # Controlled cleanup of the now-unshared owned image by ID.
    assert _cleanup_owned_image(backend_id, run.run_id) == "removed"
    assert _inspect_image(backend_id).state is not InspectState.FOUND
