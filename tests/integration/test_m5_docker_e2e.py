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


class OwnershipConflict(AssertionError):
    """Foreign resources share the namespace; adoption is refused."""


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


class _FailureLog:
    """One structured accumulator for a whole teardown lifecycle.

    Every discovery, validation, classification, cleanup and
    postcondition operation appends here; no later exception can
    discard earlier failures (R7 F4).
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.items: list[BaseException] = []

    def add(self, operation: str, image_id: str | None,
            command: str, reason: str) -> None:
        self.items.append(CleanupFailure(
            operation, image_id, self.run_id, command, reason,
            preserved=True))


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
        # R7: persistence is a unique per-run HOST bind directory
        # created by this invocation, never a named Docker volume.
        self.workspace = tmp_path / f"ws-{self.run_id}"
        self.bind_dir = self.workspace / "data"
        self.marker_file = self.bind_dir / f".trustflow-owned-{self.run_id}"
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
            "    volumes: !override\n"
            "      - type: bind\n"
            f"        source: {self.bind_dir.as_posix()}\n"
            "        target: /data\n"
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
            f"      - \"{label}\"\n",
            encoding="utf-8")
        self.base = ["docker", "compose", "-p", self.project,
                     "-f", str(ROOT / "docker-compose.yml"),
                     "-f", str(override)]
        self.preflight_inventory: dict[str, dict] = {}
        self.image_ids: dict[str, str] = {}
        self.container_ids: dict[str, str] = {}
        self.ownership_verified = False

    def preflight(self) -> None:
        _compose_preflight(self.project)  # before ANY mutation

    def _prepare_workspace(self) -> None:
        """Create the private bind workspace; refuse reused paths."""
        if self.workspace.exists() or self.workspace.is_symlink():
            raise AssertionError(
                f"workspace already exists or is a link: {self.workspace}")
        self.bind_dir.mkdir(parents=True)
        self.marker_file.write_text(self.run_id, encoding="utf-8")

    @staticmethod
    def _norm_path(path: str) -> str:
        return path.replace("\\", "/").rstrip("/").casefold()

    def start(self) -> None:
        """Build, create, verify ownership/mounts, THEN start.

        No application process runs before the ownership gate: the
        backend could otherwise initialize (and mutate) whatever
        storage Compose happened to mount (R7 F1).
        """
        self.preflight_inventory = _image_inventory()
        self._prepare_workspace()
        build = _run([*self.base, "build"])
        assert build.returncode == 0, build.stderr[-2000:]
        config = _run([*self.base, "config", "--format", "json"],
                      timeout=120)
        assert config.returncode == 0, config.stderr[-1000:]
        rendered = json.loads(config.stdout)
        mounts = (rendered["services"]["backend"].get("volumes") or [])
        assert len(mounts) == 1 and mounts[0].get("type") == "bind" \
            and self._norm_path(str(mounts[0].get("source", ""))) == \
            self._norm_path(self.bind_dir.as_posix()), mounts
        created = _run([*self.base, "create"])
        assert created.returncode == 0, created.stderr[-2000:]
        # Record exact service container identities immediately:
        # labels alone cannot prove container ownership, because a
        # container created from our image inherits its labels.
        for service in ("backend", "frontend"):
            cid = _run([*self.base, "ps", "-a", "-q", service],
                       timeout=120).stdout.strip()
            assert cid, f"container for {service} was not created"
            self.container_ids[service] = cid
        self._verify_ownership()  # gate: nothing has started yet
        started = _run([*self.base, "start"])
        assert started.returncode == 0, started.stderr[-2000:]
        self._wait_health()

    def up(self) -> None:
        self.preflight()
        self.start()

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
        cid = _run([*self.base, "ps", "-a", "-q", service]).stdout.strip()
        assert cid, service
        return cid

    # -- resource-level ownership (R6 N1) --
    # Project membership alone never proves ownership: EVERY
    # container/network/volume/image in the namespace is inspected
    # individually and must carry this invocation's run identity.

    def _discover(self, log: "_FailureLog") -> tuple[dict[str, list[dict]],
                                                     list[dict]]:
        """Classify resources as OWNED / FOREIGN; else log UNKNOWN.

        Every listed resource is strictly validated (shape, types,
        exact identity, string labels). Malformed or inconclusive
        records append a structured failure to the shared `log`
        accumulator and are preserved — never silently discarded
        (R7 F3/F4). This method never raises for resource problems.
        """
        owned: dict[str, list[dict]] = {
            "containers": [], "networks": [], "volumes": [], "images": []}
        foreign: list[dict] = []

        def fail(kind: str, cmd: str, reason: str) -> None:
            log.add(f"inventory:{kind}", None, cmd, reason)

        def query(kind: str, argv: list[str]) -> list[str] | None:
            try:
                proc = _run(argv, timeout=120)
            except Exception as exc:
                fail(kind, " ".join(argv), f"{type(exc).__name__}: {exc}")
                return None
            if proc.returncode != 0:
                fail(kind, " ".join(argv),
                     f"rc={proc.returncode}: "
                     f"{(proc.stderr or '')[-300:]}")
                return None
            return [x for x in proc.stdout.split() if x.strip()]

        def valid_labels(value: object, kind: str, ref: str) -> dict | None:
            if value is None:
                return {}
            if not isinstance(value, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in value.items()):
                fail(kind, f"docker inspect {ref}",
                     f"malformed metadata: labels must be a "
                     f"string->string object, got {type(value).__name__}")
                return None
            return dict(value)

        def classify(kind: str, record: dict, ref: str) -> None:
            labels = record["labels"]
            if labels.get(PROJECT_LABEL) == self.project \
                    and labels.get(RUN_LABEL) == self.run_id:
                owned[kind].append(record)
            elif labels.get(PROJECT_LABEL) == self.project:
                foreign.append({"kind": kind, **record})
            else:
                fail(kind, f"docker inspect {ref}",
                     "unclassifiable resource: labels do not confirm "
                     f"project {self.project}: {labels}")

        def identity(raw: object, kind: str, ref: str,
                     field: str = "Id") -> str | None:
            if not isinstance(raw, dict):
                fail(kind, f"docker inspect {ref}",
                     f"malformed metadata: expected object, "
                     f"got {type(raw).__name__}")
                return None
            value = raw.get(field)
            if not isinstance(value, str) or not value \
                    or (value != ref and not value.startswith(ref)
                        and not ref.startswith(value)):
                fail(kind, f"docker inspect {ref}",
                     f"malformed metadata: {field}={value!r} does "
                     f"not match requested identity {ref!r}")
                return None
            return value

        pf = f"label={PROJECT_LABEL}={self.project}"
        ids = query("containers",
                    ["docker", "ps", "-a", "-q", "--filter", pf])
        for cid in ids or []:
            try:
                raw = _raw_inspect(cid)
                rid = identity(raw, "containers", cid)
                if rid is None:
                    continue
                config = raw.get("Config") if isinstance(raw, dict) \
                    else None
                if not isinstance(config, dict):
                    fail("containers", f"docker inspect {cid}",
                         "malformed metadata: Config must be an object")
                    continue
                labels = valid_labels(config.get("Labels"),
                                      "containers", cid)
                if labels is None:
                    continue
                classify("containers", {
                    "id": rid, "name": raw.get("Name"),
                    "labels": labels,
                    "running": bool(
                        (raw.get("State") or {}).get("Running"))}, cid)
            except Exception as exc:
                fail("containers", f"docker inspect {cid}",
                     f"{type(exc).__name__}: {exc}")
        ids = query("networks",
                    ["docker", "network", "ls", "-q", "--filter", pf])
        for nid in ids or []:
            try:
                raw = _raw_inspect(nid)
                rid = identity(raw, "networks", nid)
                if rid is None:
                    continue
                labels = valid_labels(
                    raw.get("Labels") if isinstance(raw, dict) else None,
                    "networks", nid)
                if labels is None:
                    continue
                classify("networks", {
                    "id": rid, "name": raw.get("Name"),
                    "labels": labels}, nid)
            except Exception as exc:
                fail("networks", f"docker network inspect {nid}",
                     f"{type(exc).__name__}: {exc}")
        names = query("volumes",
                      ["docker", "volume", "ls", "-q", "--filter", pf])
        for name in names or []:
            try:
                raw = _raw_inspect(name)
                rid = identity(raw, "volumes", name, field="Name")
                if rid is None:
                    continue
                labels = valid_labels(
                    raw.get("Labels") if isinstance(raw, dict) else None,
                    "volumes", name)
                if labels is None:
                    continue
                classify("volumes", {
                    "id": rid, "name": name, "labels": labels}, name)
            except Exception as exc:
                fail("volumes", f"docker volume inspect {name}",
                     f"{type(exc).__name__}: {exc}")
        ids = query("images",
                    ["docker", "image", "ls", "-q", "--no-trunc",
                     "--filter", pf])
        for ref in ids or []:
            try:
                res = _inspect_image(ref)
            except Exception as exc:
                fail("images", f"docker image inspect {ref}",
                     f"{type(exc).__name__}: {exc}")
                continue
            if res.state is not InspectState.FOUND or not res.image_id:
                fail("images", f"docker image inspect {ref}",
                     f"state={res.state.value}: {res.detail}")
                continue
            classify("images", {"id": res.image_id, "name": ref,
                                "labels": res.labels}, ref)
        return owned, foreign

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
        # Pre-start gate (R7 F1): containers must not be running,
        # and actual mounts must be exactly the invocation-owned
        # bind directory — verified from real container metadata.
        for service, cid in self.container_ids.items():
            raw = _raw_inspect(cid)
            assert not (raw.get("State") or {}).get("Running"), \
                f"{service} started before the ownership gate"
            mounts = raw.get("Mounts") or []
            if service == "backend":
                data = [m for m in mounts
                        if m.get("Destination") == "/data"]
                assert len(data) == 1 and data[0].get("Type") == "bind" \
                    and self._norm_path(str(data[0].get("Source", ""))) \
                    == self._norm_path(self.bind_dir.as_posix()), mounts
            else:
                assert not [m for m in mounts
                            if m.get("Destination") == "/data"], mounts
        assert self.marker_file.read_text(encoding="utf-8").strip() \
            == self.run_id
        # Every namespace resource — networks and volumes included —
        # must belong to this invocation before any workflow runs.
        gate_log = _FailureLog(self.run_id)
        _owned, foreign = self._discover(gate_log)
        failures = gate_log.items
        if failures:
            raise AssertionError(
                f"ownership inventory inconclusive for project "
                f"{self.project} run={self.run_id}: {failures}")
        if foreign:
            raise OwnershipConflict(
                f"foreign resources in namespace {self.project} "
                f"run={self.run_id}: "
                f"{[(f['kind'], f['id'], f['name']) for f in foreign]}; "
                f"adoption refused, resources preserved")
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

    def _cleanup_workspace(self, fail) -> None:
        """Delete ONLY the positively identified private workspace.

        Identity proof: path is the exact per-run directory created
        by this invocation, it is not a symlink/reparse point, the
        ownership marker names this run, and no recorded container
        still exists to hold the mount. Anything else is preserved.
        """
        import shutil as _shutil

        target = self.workspace
        try:
            if target.is_symlink() or self.bind_dir.is_symlink():
                fail("workspace-cleanup", None, str(target),
                     "workspace path is a symlink/reparse point; "
                     "preserved")
                return
            if not self.marker_file.is_file() or \
                    self.marker_file.read_text(
                        encoding="utf-8").strip() != self.run_id:
                fail("workspace-cleanup", None, str(target),
                     "ownership marker missing or mismatched; "
                     "preserved")
                return
            for cid in self.container_ids.values():
                proc = _run(["docker", "inspect", "-f", "{{.Id}}", cid],
                            timeout=120)
                if proc.returncode == 0:
                    fail("workspace-cleanup", None, str(target),
                         f"container {cid} still exists; workspace "
                         f"preserved")
                    return
            _shutil.rmtree(target)
            if target.exists():
                fail("workspace-cleanup", None, str(target),
                     "workspace still present after cleanup")
        except BaseException as exc:
            fail("workspace-cleanup", None, str(target),
                 f"{type(exc).__name__}: {exc}")

    def teardown(self) -> list[BaseException]:
        """Resource-scoped verified teardown; returns failures.

        No project-wide `down -v`: every container/network/volume/
        image target was individually discovered and label-verified
        as this invocation's own. Foreign resources are preserved
        and reported. The whole lifecycle is guarded: no discovery,
        inspection or cleanup exception can erase accumulated
        failures or the caller's primary error (R6 N1/N2/N3). A
        residual check-then-delete window on a shared daemon is
        documented in docs/m5.md; it is not claimed race-free.
        """
        log = _FailureLog(self.run_id)
        failures = log.items

        def fail(op: str, image_id: str | None, cmd: str, reason: str):
            log.add(op, image_id, cmd, reason)

        try:
            owned, foreign = self._discover(log)
        except BaseException as exc:  # discovery itself exploded
            fail("inventory", None, "resource discovery",
                 f"{type(exc).__name__}: {exc}")
            return failures
        for record in foreign:
            fail("ownership", record.get("id"),
                 "resource discovery",
                 f"foreign {record['kind']} preserved: "
                 f"id={record.get('id')} name={record.get('name')} "
                 f"labels={record.get('labels')}")
        if not self.ownership_verified \
                and not any(owned.values()):
            return failures  # never owned: nothing may be destroyed

        def run_op(op: str, image_id: str | None, argv: list[str]) -> bool:
            try:
                proc = _run(argv, timeout=300)
            except BaseException as exc:
                fail(op, image_id, " ".join(argv),
                     f"{type(exc).__name__}: {exc}")
                return False
            if proc.returncode != 0:
                fail(op, image_id, " ".join(argv),
                     f"rc={proc.returncode}: "
                     f"{(proc.stderr or '')[-300:]}")
                return False
            return True

        # Containers: only recorded service identities, re-verified
        # by labels. A container created from our image inherits its
        # labels, so discovered-but-unrecorded containers are
        # preserved and reported, never deleted on label evidence.
        recorded = set(self.container_ids.values())
        for rec in owned["containers"]:
            cid = rec["id"]
            if cid not in recorded:
                fail("ownership:container", cid, "resource discovery",
                     "unrecorded container with inherited labels "
                     "preserved (not created as a service container)")
                continue
            if rec.get("running"):
                run_op("container-stop", cid, ["docker", "stop", cid])
            run_op("container-rm", cid, ["docker", "rm", cid])
        for rec in owned["networks"]:
            run_op("network-rm", rec["id"],
                   ["docker", "network", "rm", rec["id"]])
        for rec in owned["volumes"]:
            # R7: named Docker volumes are NEVER deleted by this
            # lifecycle (mutable-name teardown is eliminated). An
            # owned-classified named volume is unexpected under the
            # bind-mount design: preserve it and fail visibly.
            fail("ownership:volume", None,
                 "resource discovery",
                 f"unexpected owned named volume preserved (named "
                 f"volumes are never deleted): name={rec['name']} "
                 f"id={rec['id']}")
        targets: dict[str, str] = dict(self.image_ids)
        for rec in owned["images"]:
            targets.setdefault(rec["id"], rec["id"])
        for image_id in dict.fromkeys(targets.values()):
            try:
                outcome = _cleanup_owned_image(image_id, self.run_id)
            except BaseException as exc:
                fail("image-cleanup", image_id,
                     f"docker rmi {image_id}",
                     f"{type(exc).__name__}: {exc}")
                continue
            if outcome not in ("removed", "absent"):
                fail("image-cleanup", image_id,
                     f"docker rmi {image_id}", f"outcome={outcome}")
        # Workspace cleanup: only after containers are gone, and
        # only if the private directory identity is provable.
        self._cleanup_workspace(fail)
        # Postconditions: only a successful authoritative inventory
        # with zero owned leftovers satisfies mandatory cleanup.
        try:
            left, _foreign_left = self._discover(log)
            for kind in ("containers", "networks", "images"):
                if left[kind]:
                    fail(f"postcondition:{kind}", None,
                         "resource discovery",
                         f"owned still present: "
                         f"{[r['id'] for r in left[kind]]}")
            for image_id in self.image_ids.values():
                try:
                    state = _inspect_image(image_id).state
                except BaseException as exc:
                    fail("postcondition:image", image_id,
                         f"docker image inspect {image_id}",
                         f"{type(exc).__name__}: {exc}")
                    continue
                if state is InspectState.FOUND:
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
            fail("postcondition", None, "resource discovery",
                 f"{type(exc).__name__}: {exc}")
        return failures


def _run_stack_test(tmp_path: Path, *, project: str | None = None,
                    run_id: str | None = None,
                    before_start=None) -> _StackRun:
    """Full candidate entrypoint with total failure preservation.

    `before_start` (test hook) runs after preflight, modelling a
    foreign invocation racing the namespace. Teardown itself is
    guarded so its failures can never replace the primary (N3).
    """
    run = _StackRun(tmp_path, project=project, run_id=run_id)
    primary: BaseException | None = None
    try:
        run.preflight()
        if before_start is not None:
            before_start(run)
        run.start()
        run.exercise()
    except BaseException as exc:
        primary = exc
    failures: list[BaseException] = []
    if not isinstance(primary, NamespaceConflict):
        try:
            failures = run.teardown()
        except BaseException as exc:
            failures = [CleanupFailure(
                "teardown", None, run.run_id, "stack teardown",
                f"{type(exc).__name__}: {exc}", preserved=True)]
    _raise_stack_result(primary, failures)
    return run


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
            _remove_own_volume(vol_name, {PROJECT_LABEL: victim})
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


def _exception_graph_text(exc: BaseException) -> str:
    parts = [str(exc)]
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            parts.append(_exception_graph_text(child))
    if exc.__cause__ is not None:
        parts.append(_exception_graph_text(exc.__cause__))
    return "\n".join(parts)


def _remove_own_volume(name: str, expected_labels: dict) -> None:
    """Remove a test-fixture volume ONLY after label re-verification.

    This is the single volume-removal helper for fixtures this test
    itself created; the candidate lifecycle never calls it.
    """
    raw = _raw_inspect(name)
    labels = raw.get("Labels") or {}
    for key, value in expected_labels.items():
        assert labels.get(key) == value, labels
    proc = _run(["docker", "volume", "rm", name], timeout=120)
    assert proc.returncode == 0, proc.stderr


def _create_foreign_resources(project: str) -> tuple[str, str, str]:
    """Foreign network+volume in `project` under another run ID.

    Returns (foreign_run, network_name, volume_name). Resources are
    created and removed by the calling test (its own fixtures).
    """
    foreign_run = f"other-{uuid.uuid4().hex}"
    net_name, vol_name = f"{project}_default", f"{project}_tf-data"
    proc = _run(["docker", "network", "create",
                 "--label", f"{PROJECT_LABEL}={project}",
                 "--label", f"{RUN_LABEL}={foreign_run}", net_name])
    assert proc.returncode == 0, proc.stderr
    proc = _run(["docker", "volume", "create",
                 "--label", f"{PROJECT_LABEL}={project}",
                 "--label", f"{RUN_LABEL}={foreign_run}", vol_name])
    assert proc.returncode == 0, proc.stderr
    return foreign_run, net_name, vol_name


def _assert_foreign_intact(net_name: str, vol_name: str,
                           foreign_run: str) -> None:
    net = _raw_inspect(net_name)
    assert (net.get("Labels") or {}).get(RUN_LABEL) == foreign_run
    vol = _raw_inspect(vol_name)
    assert (vol.get("Labels") or {}).get(RUN_LABEL) == foreign_run


def test_stack_foreign_resources_race_full(tmp_path):
    """N1-FULL: foreign net/volume appearing after preflight are
    never adopted or deleted; ownership conflict fails the test."""
    project = f"tf-m5-it-{uuid.uuid4().hex}"
    holder: dict = {}

    def plant_foreign(run: _StackRun) -> None:
        holder["foreign"] = _create_foreign_resources(project)

    try:
        with pytest.raises(BaseException) as excinfo:
            _run_stack_test(tmp_path, project=project,
                            before_start=plant_foreign)
        assert "foreign" in _exception_graph_text(excinfo.value)
        foreign_run, net_name, vol_name = holder["foreign"]
        _assert_foreign_intact(net_name, vol_name, foreign_run)
    finally:
        if "foreign" in holder:
            foreign_run, net_name, vol_name = holder["foreign"]
            _run(["docker", "network", "rm", net_name], timeout=120)
            _remove_own_volume(vol_name, {
                PROJECT_LABEL: project, RUN_LABEL: foreign_run})


def test_stack_foreign_resources_race_partial(tmp_path):
    """N1-PARTIAL: partial setup cleans only its own verified image."""
    project = f"tf-m5-it-{uuid.uuid4().hex}"
    run = _StackRun(tmp_path, project=project)
    run.preflight()
    foreign_run, net_name, vol_name = _create_foreign_resources(project)
    try:
        built = _run([*run.base, "build", "backend"])
        assert built.returncode == 0, built.stderr[-1000:]
        primary = AssertionError("PRIMARY_PARTIAL_BUILD_MARKER")
        failures = run.teardown()
        _assert_foreign_intact(net_name, vol_name, foreign_run)
        # Only the owned backend image may be gone; nothing foreign.
        owned_left = run._discover(_FailureLog(run.run_id))[0]["images"]
        assert owned_left == []
        with pytest.raises(BaseException) as excinfo:
            _raise_stack_result(primary, failures)
        assert "PRIMARY_PARTIAL_BUILD_MARKER" in str(
            excinfo.value.__cause__ or excinfo.value)
    finally:
        _run(["docker", "network", "rm", net_name], timeout=120)
        _remove_own_volume(vol_name, {
            PROJECT_LABEL: project, RUN_LABEL: foreign_run})


def _foreign_sqlite_volume(project: str) -> tuple[str, str, str]:
    """Plant a foreign volume with sentinel file + sentinel SQLite.

    Returns (foreign_run, volume_name, db_sha256). All resources are
    created by the calling test and removed by it afterwards.
    """
    foreign_run = f"other-{uuid.uuid4().hex}"
    vol_name = f"{project}_tf-data"
    proc = _run(["docker", "volume", "create",
                 "--label", f"{PROJECT_LABEL}={project}",
                 "--label", f"{RUN_LABEL}={foreign_run}", vol_name])
    assert proc.returncode == 0, proc.stderr
    script = (
        "import sqlite3, pathlib; "
        "pathlib.Path('/data/sentinel.txt').write_text('foreign-sentinel'); "
        "db = sqlite3.connect('/data/trustflow.db'); "
        "db.execute('CREATE TABLE reviewer_sentinel (v TEXT)'); "
        "db.execute(\"INSERT INTO reviewer_sentinel VALUES ('keep-me')\"); "
        "db.commit(); db.close()")
    made = _run(["docker", "run", "--rm", "--mount",
                 f"source={vol_name},target=/data",
                 "trustflow-backend:latest", "python", "-c", script],
                timeout=300)
    assert made.returncode == 0, made.stderr[-1000:]
    digest = _run(["docker", "run", "--rm", "--mount",
                   f"source={vol_name},target=/data",
                   "trustflow-backend:latest", "python", "-c",
                   "import hashlib; print(hashlib.sha256("
                   "open('/data/trustflow.db','rb').read()).hexdigest())"],
                  timeout=300)
    assert digest.returncode == 0, digest.stderr[-500:]
    return foreign_run, vol_name, digest.stdout.strip()


def test_stack_foreign_sqlite_never_started_against(tmp_path):
    """F1: the backend must never start against foreign storage.

    A foreign named volume holds a sentinel SQLite DB. The bind
    candidate's pre-start gate must conflict before any start, so
    the foreign bytes are byte-identical afterwards and the
    candidate's own bind directory was never initialized.
    """
    project = f"tf-m5-it-{uuid.uuid4().hex}"
    foreign_run, vol_name, sha_before = _foreign_sqlite_volume(project)
    proc = _run(["docker", "network", "create",
                 "--label", f"{PROJECT_LABEL}={project}",
                 "--label", f"{RUN_LABEL}={foreign_run}",
                 f"{project}_default"])
    assert proc.returncode == 0, proc.stderr
    run = _StackRun(tmp_path, project=project)
    primary: BaseException | None = None
    try:
        run.preflight()
        run.start()
    except BaseException as exc:
        primary = exc
    failures = run.teardown()
    try:
        assert primary is not None
        graph = _exception_graph_text(primary)
        for failure in failures:
            graph += "\n" + str(failure)
        assert "foreign" in graph
        digest = _run(["docker", "run", "--rm", "--mount",
                       f"source={vol_name},target=/data",
                       "trustflow-backend:latest", "python", "-c",
                       "import hashlib; print(hashlib.sha256("
                       "open('/data/trustflow.db','rb')"
                       ".read()).hexdigest())"], timeout=300)
        assert digest.stdout.strip() == sha_before
        sentinel = _run(["docker", "run", "--rm", "--mount",
                         f"source={vol_name},target=/data",
                         "trustflow-backend:latest", "python", "-c",
                         "print(open('/data/sentinel.txt').read())"],
                        timeout=300)
        assert sentinel.stdout.strip() == "foreign-sentinel"
        # The candidate backend never initialized any database.
        assert not (run.bind_dir / "trustflow.db").exists()
        _assert_foreign_intact(f"{project}_default", vol_name,
                               foreign_run)
    finally:
        _run(["docker", "network", "rm", f"{project}_default"],
             timeout=120)
        _remove_own_volume(vol_name, {
            PROJECT_LABEL: project, RUN_LABEL: foreign_run})


def test_stack_inventory_failure_fails_closed(tmp_path, monkeypatch):
    """N2: real leftover network + injected inventory failure.

    The full candidate function must FAIL (never false-PASS) while
    the held network and its holder container are preserved.
    """
    import subprocess as _subprocess
    import sys as _sys

    real_run = _run
    state: dict = {"project": None, "holder": None, "net_name": None,
                   "teardown_phase": False}

    def wrapper(args, timeout=600, cwd=None):
        argv = list(args)
        if argv[:2] == ["docker", "compose"] and "-p" in argv:
            state["project"] = argv[argv.index("-p") + 1]
        if "restart" in argv:  # exercise midpoint: teardown comes next
            state["teardown_phase"] = True
        if state["teardown_phase"] \
                and argv[:3] == ["docker", "network", "ls"]:
            if state["holder"] is None:
                nets = real_run(
                    ["docker", "network", "ls", "--filter",
                     f"label={PROJECT_LABEL}={state['project']}",
                     "--format", "{{.Name}}"], timeout=120).stdout.split()
                state["net_name"] = nets[0] if nets else ""
                # Holder references the stack's own backend image.
                image = real_run(
                    ["docker", "image", "ls", "-q", "--no-trunc",
                     "--filter",
                     f"label={PROJECT_LABEL}={state['project']}"],
                    timeout=120).stdout.split()[0]
                made = real_run(
                    ["docker", "create", "--network", state["net_name"],
                     image], timeout=120)
                state["holder_debug"] = (
                    f"nets={nets} image={image} rc={made.returncode} "
                    f"err={(made.stderr or '')[-200:]}")
                state["holder"] = made.stdout.strip()
            return _subprocess.CompletedProcess(
                argv, 1, "", "NETWORK_INVENTORY_DENIED_MARKER")
        return real_run(argv, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(_sys.modules[__name__], "_run", wrapper)
    try:
        with pytest.raises(BaseException) as excinfo:
            _run_stack_test(tmp_path)
        assert "NETWORK_INVENTORY_DENIED_MARKER" in \
            _exception_graph_text(excinfo.value)
        # Held network and holder container are preserved.
        assert state["holder"] and state["net_name"], \
            state.get("holder_debug")
        alive = real_run(["docker", "inspect", "-f", "{{.Id}}",
                          state["holder"]], timeout=120)
        assert alive.returncode == 0
        nets = real_run(["docker", "network", "ls", "--filter",
                         f"label={PROJECT_LABEL}={state['project']}",
                         "--format", "{{.Name}}"],
                        timeout=120).stdout
        assert state["net_name"] in nets
    finally:
        monkeypatch.undo()
        # Controlled cleanup of this test's own leftovers.
        if state.get("holder"):
            real_run(["docker", "rm", "-f", state["holder"]],
                     timeout=120)
        if state.get("project"):
            project = state["project"]
            for cid in real_run(
                    ["docker", "ps", "-a", "-q", "--filter",
                     f"label={PROJECT_LABEL}={project}"],
                    timeout=120).stdout.split():
                real_run(["docker", "stop", cid], timeout=120)
                real_run(["docker", "rm", cid], timeout=120)
            for name in (f"{project}_default",):
                real_run(["docker", "network", "rm", name],
                         timeout=120)
            # No named volume is created by the bind-mount candidate;
            # nothing volume-shaped is removed here.
            for image_id in real_run(
                    ["docker", "image", "ls", "-q", "--no-trunc",
                     "--filter", f"label={PROJECT_LABEL}={project}"],
                    timeout=120).stdout.split():
                real_run(["docker", "rmi", image_id], timeout=120)


def test_stack_malformed_network_metadata_fails(tmp_path, monkeypatch):
    """F3: inspect `[{}]` for a real held network must FAIL.

    During teardown, inspect of the candidate's owned network is
    replaced with a successful but malformed `[{}]` response while
    a review-owned holder keeps the network alive. The network must
    be preserved and the test must FAIL with a malformed-metadata
    failure; no false empty inventory may result.
    """
    import subprocess as _subprocess
    import sys as _sys

    real_run = _run
    state: dict = {"project": None, "holder": None, "net_name": None,
                   "teardown_phase": False, "malformed_done": False}

    def wrapper(args, timeout=600, cwd=None):
        argv = list(args)
        if argv[:2] == ["docker", "compose"] and "-p" in argv:
            state["project"] = argv[argv.index("-p") + 1]
        if "restart" in argv:
            state["teardown_phase"] = True
        if state["teardown_phase"] and argv[:2] == ["docker", "inspect"]:
            target = argv[2] if len(argv) > 2 else ""
            nets = real_run(
                ["docker", "network", "ls", "--filter",
                 f"label={PROJECT_LABEL}={state['project']}",
                 "--format", "{{.ID}}"], timeout=120).stdout.split()
            if nets and target in nets \
                    and not state["malformed_done"]:
                state["malformed_done"] = True
                state["net_name"] = real_run(
                    ["docker", "network", "ls", "--filter",
                     f"label={PROJECT_LABEL}={state['project']}",
                     "--format", "{{.Name}}"],
                    timeout=120).stdout.split()[0]
                image = real_run(
                    ["docker", "image", "ls", "-q", "--no-trunc",
                     "--filter",
                     f"label={PROJECT_LABEL}={state['project']}"],
                    timeout=120).stdout.split()[0]
                made = real_run(
                    ["docker", "create", "--network", state["net_name"],
                     image], timeout=120)
                state["holder"] = made.stdout.strip()
                return _subprocess.CompletedProcess(argv, 0, "[{}]", "")
        return real_run(argv, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(_sys.modules[__name__], "_run", wrapper)
    try:
        with pytest.raises(BaseException) as excinfo:
            _run_stack_test(tmp_path)
        graph = _exception_graph_text(excinfo.value)
        assert "malformed" in graph
        assert state["malformed_done"] is True
        assert state["holder"] and state["net_name"]
        alive = real_run(["docker", "inspect", "-f", "{{.Id}}",
                          state["holder"]], timeout=120)
        assert alive.returncode == 0
        nets = real_run(["docker", "network", "ls", "--filter",
                         f"label={PROJECT_LABEL}={state['project']}",
                         "--format", "{{.Name}}"],
                        timeout=120).stdout
        assert state["net_name"] in nets
    finally:
        monkeypatch.undo()
        if state.get("holder"):
            real_run(["docker", "rm", "-f", state["holder"]],
                     timeout=120)
        if state.get("project"):
            project = state["project"]
            for cid in real_run(
                    ["docker", "ps", "-a", "-q", "--filter",
                     f"label={PROJECT_LABEL}={project}"],
                    timeout=120).stdout.split():
                real_run(["docker", "stop", cid], timeout=120)
                real_run(["docker", "rm", cid], timeout=120)
            real_run(["docker", "network", "rm", f"{project}_default"],
                     timeout=120)
            for image_id in real_run(
                    ["docker", "image", "ls", "-q", "--no-trunc",
                     "--filter", f"label={PROJECT_LABEL}={project}"],
                    timeout=120).stdout.split():
                real_run(["docker", "rmi", image_id], timeout=120)
