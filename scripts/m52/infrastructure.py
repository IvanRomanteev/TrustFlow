"""M5.2 adapter around the accepted P2-05 Docker ownership lifecycle.

The accepted module is loaded unchanged, in an instance-local namespace. Only
its command executor is replaced with an offline, sanitizing observer. Resource
discovery, filesystem identity gates and teardown remain its implementation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

TOKEN = "demo-approval-token"
_SECRET_NAME = re.compile(r"key|token|password|secret|credential", re.IGNORECASE)
_SENSITIVE_FIELD = re.compile(
    r"api.?key|approval.?token|password|secret|credential|^authorization$", re.IGNORECASE)
_SQLITE_NAME = re.compile(r".+\.db(?:-(?:wal|shm|journal))?$")


def offline_environment() -> dict[str, str]:
    """Keep OS/Docker tooling configuration while excluding model credentials."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("TRUSTFLOW_") and not _SECRET_NAME.search(key)
    }
    env.update({"TRUSTFLOW_LLM_MODE": "fake", "MODEL_API_KEY": "offline-fake-placeholder"})
    return env


def sanitize(value: Any) -> Any:
    """Never publish Docker Config.Env or approval/credential values."""
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if str(key).lower() == "env" or
                       _SENSITIVE_FIELD.search(str(key)) else sanitize(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        text = value.replace(TOKEN, "[REDACTED]")
        # Compose config uses environment values as dictionary members; JSON
        # observations are sanitized structurally before reaching this path.
        return re.sub(
            r"(?i)((?:api[_-]?key|approval[_-]?token|password|secret)\s*[=:]\s*)"
            r"[^\s,;]+", r"\1[REDACTED]", text)
    return value


def _safe_output(text: str) -> Any:
    try:
        return sanitize(json.loads(text))
    except (ValueError, TypeError):
        return sanitize(text)


def _load_accepted(root: Path, run_id: str):
    path = root / "tests/integration/test_m5_docker_e2e.py"
    spec = importlib.util.spec_from_file_location(f"_m52_accepted_{run_id}", path)
    assert spec and spec.loader, f"cannot load accepted lifecycle: {path}"
    module = importlib.util.module_from_spec(spec)
    # Dataclasses inspect their defining module at decoration time.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DemoStack:
    """Real Docker stack with accepted ownership and immutable-ID cleanup."""

    def __init__(
        self, root: Path, out: Path, run_id: str,
        journal: Callable[[dict], None] | None = None,
        prepare: Callable[[Any], None] | None = None,
        backend_dockerfile: Path | None = None,
        backend_command: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9]{12,64}", run_id):
            raise ValueError("run_id must be 12-64 lowercase alphanumeric characters")
        self.root, self.out, self.run_id = root.resolve(), out.resolve(), run_id
        self.project = f"tf-m52-{run_id}"
        self.commands: list[dict] = []
        self._journal = journal
        self._module = _load_accepted(self.root, run_id)
        self._module._run = self.run
        accepted_class = self._module._StackRun

        class PreparedStack(accepted_class):
            def _prepare_workspace(inner_self) -> None:
                super()._prepare_workspace()
                if prepare is not None:
                    inner_self._validate_workspace()
                    prepare(inner_self)
                    inner_self._validate_workspace()

        self._stack = PreparedStack(self.out, project=self.project, run_id=run_id)
        # _free_port is advisory, so fail on a collision before any build/create
        # and again immediately before the accepted create/start sequence.
        if self.backend_port == self.frontend_port:
            raise AssertionError("dynamic port allocation collision; rerun with a new invocation")
        if backend_dockerfile or backend_command or backend_env:
            backend: dict[str, Any] = {}
            if backend_dockerfile:
                backend["build"] = {"dockerfile": str(backend_dockerfile.resolve())}
            if backend_command:
                backend["command"] = backend_command
            if backend_env:
                assert all(isinstance(k, str) and isinstance(v, str)
                           for k, v in backend_env.items())
                backend["environment"] = backend_env
            override = self.out / f"demo-backend-{run_id}.json"
            override.write_text(json.dumps({"services": {"backend": backend}}, indent=2),
                                encoding="utf-8")
            self._stack.base += ["-f", str(override)]

    @property
    def backend_port(self) -> int:
        return self._stack.backend_port

    @property
    def frontend_port(self) -> int:
        return self._stack.frontend_port

    @property
    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.backend_port}"

    @property
    def frontend_url(self) -> str:
        return f"http://127.0.0.1:{self.frontend_port}"

    @property
    def bind_dir(self) -> Path:
        return self._stack.bind_dir

    @property
    def container_ids(self) -> dict[str, str]:
        return dict(self._stack.container_ids)

    def _record(self, record: dict) -> None:
        safe = sanitize(record)
        self.commands.append(safe)
        with (self.out / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, ensure_ascii=False) + "\n")
        if self._journal:
            self._journal(safe)

    def run(
        self, args: list[str], timeout: int = 600, cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        started = time.monotonic()
        try:
            if args[:2] == ["docker", "compose"] and args[-1] in {"create", "start"}:
                self._check_ports_free()
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                  cwd=cwd or self.root, env=offline_environment(),
                                  encoding="utf-8", errors="replace")
        except BaseException as exc:
            self._record({"argv": args, "cwd": str(cwd or self.root), "exit_code": None,
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "exception": f"{type(exc).__name__}: {exc}"})
            raise
        self._record({"argv": args, "cwd": str(cwd or self.root),
                      "exit_code": proc.returncode,
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "stdout": _safe_output(proc.stdout), "stderr": _safe_output(proc.stderr)})
        return proc

    def _json(self, argv: list[str]) -> Any:
        proc = self.run(argv, timeout=120)
        assert proc.returncode == 0, f"{argv}: {sanitize(proc.stderr)}"
        return json.loads(proc.stdout)

    def _check_ports_free(self) -> None:
        for port in (self.backend_port, self.frontend_port):
            with socket.socket() as listener:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(("127.0.0.1", port))

    def up(self) -> None:
        self._check_ports_free()
        self._stack.up()
        self.verify_bindings()

    def verify_bindings(self) -> dict:
        proc = self.run([*self._stack.base, "config", "--format", "json"], timeout=120)
        assert proc.returncode == 0, sanitize(proc.stderr)
        config = json.loads(proc.stdout)
        services = config["services"]
        api_url = services["frontend"]["build"]["args"]["NEXT_PUBLIC_API_URL"]
        assert api_url == self.backend_url, api_url
        origins = json.loads(services["backend"]["environment"]["TRUSTFLOW_CORS_ORIGINS"])
        assert self.frontend_url in origins, origins
        assert services["backend"]["environment"]["TRUSTFLOW_LLM_MODE"] == "fake"
        observed = {}
        for service, host_port, container_port in (
            ("backend", self.backend_port, 8000), ("frontend", self.frontend_port, 3000),
        ):
            ports = services[service]["ports"]
            assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1"
            assert int(ports[0]["published"]) == host_port
            assert int(ports[0]["target"]) == container_port
            cid = self._owned_container(service)
            raw = self._json(["docker", "container", "inspect", cid])[0]
            bindings = raw["NetworkSettings"]["Ports"][f"{container_port}/tcp"]
            assert bindings and all(
                b.get("HostIp") == "127.0.0.1" and int(b["HostPort"]) == host_port
                for b in bindings), bindings
            observed[service] = {"container_id": cid, "bindings": bindings}
        result = {"api_url": api_url, "cors_origins": origins, "services": observed}
        (self.out / "runtime-bindings.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        return result

    def _owned_container(self, service: str = "backend") -> str:
        assert self._stack.ownership_verified, "stack has not passed ownership gate"
        recorded = self._stack.container_ids.get(service)
        assert recorded and re.fullmatch(r"[0-9a-f]{64}", recorded)
        cid = self._stack._container_id(service)
        assert cid == recorded, "container identity changed"
        self._stack._validate_workspace()
        return cid

    def backend_lifecycle(self, action: str) -> None:
        if action not in {"stop", "start", "kill", "restart"}:
            raise ValueError(f"unsupported backend lifecycle action: {action}")
        cid = self._owned_container()
        argv = ["docker", action]
        if action == "kill":
            argv += ["--signal", "KILL"]
        proc = self.run([*argv, cid], timeout=120)
        assert proc.returncode == 0, sanitize(proc.stderr)
        if action in {"start", "restart"}:
            self._stack._wait_health()
        raw = self._json(["docker", "container", "inspect", cid])[0]
        assert raw["Id"] == cid
        assert raw["State"]["Running"] == (action in {"start", "restart"})

    def runtime_logs(self) -> dict[str, dict]:
        logs = {}
        for service in self.container_ids:
            cid = self._owned_container(service)
            proc = self.run(["docker", "logs", cid], timeout=120)
            logs[service] = sanitize({"exit_code": proc.returncode,
                                      "stdout": proc.stdout, "stderr": proc.stderr})
        (self.out / "runtime-logs.json").write_text(
            json.dumps(logs, indent=2), encoding="utf-8")
        return logs

    def teardown(self) -> list[str]:
        failures = [str(sanitize(str(failure))) for failure in self._stack.teardown()]
        if self._stack.workspace.exists() and not self._stack._workspace_cleaned:
            failures.append(
                f"accepted lifecycle preserved private workspace: {self._stack.workspace}")
        return failures

    def _ids(self, argv: list[str]) -> list[str]:
        proc = self.run(argv, timeout=120)
        assert proc.returncode == 0, sanitize(proc.stderr)
        return sorted(set(proc.stdout.split()))

    def inventory(self) -> dict:
        """Stable Docker inventory, excluding Config.Env and changing durations."""
        result: dict[str, dict] = {kind: {} for kind in (
            "containers", "images", "networks", "volumes")}
        for cid in self._ids(["docker", "ps", "-a", "-q", "--no-trunc"]):
            raw = self._json(["docker", "container", "inspect", cid])[0]
            assert raw["Id"] == cid
            result["containers"][cid] = sanitize({
                "name": raw["Name"], "image": raw["Image"], "state": raw["State"],
                "labels": raw["Config"].get("Labels"), "mounts": raw["Mounts"],
                "ports": raw["NetworkSettings"].get("Ports"),
                "networks": raw["NetworkSettings"].get("Networks"),
            })
        for iid in self._ids(["docker", "image", "ls", "-q", "--no-trunc"]):
            raw = self._json(["docker", "image", "inspect", iid])[0]
            assert raw["Id"] == iid
            result["images"][iid] = sanitize({
                "tags": sorted(raw.get("RepoTags") or []),
                "digests": sorted(raw.get("RepoDigests") or []),
                "labels": raw["Config"].get("Labels"), "rootfs": raw.get("RootFS"),
            })
        for nid in self._ids(["docker", "network", "ls", "-q", "--no-trunc"]):
            raw = self._json(["docker", "network", "inspect", nid])[0]
            assert raw["Id"] == nid
            result["networks"][nid] = sanitize({
                key: raw.get(key) for key in (
                    "Name", "Driver", "Scope", "Internal", "Attachable", "IPAM",
                    "Labels", "Containers", "Options")
            })
        for name in self._ids(["docker", "volume", "ls", "-q"]):
            raw = self._json(["docker", "volume", "inspect", name])[0]
            assert raw["Name"] == name
            result["volumes"][name] = sanitize({
                key: raw.get(key) for key in (
                    "Name", "Driver", "Mountpoint", "Labels", "Options", "Scope")
            })
        return result

    def _container_sqlite(self, inventory: dict) -> dict:
        result = {}
        for cid, data in inventory["containers"].items():
            if not any(m.get("Destination") == "/data" for m in data["mounts"]):
                continue
            argv = ["docker", "cp", f"{cid}:/data/.", "-"]
            proc = subprocess.run(argv, capture_output=True, timeout=120, cwd=self.root,
                                  env=offline_environment())
            self._record({"argv": argv, "cwd": str(self.root), "exit_code": proc.returncode,
                          "stdout": {"binary_bytes": len(proc.stdout)},
                          "stderr": proc.stderr.decode("utf-8", errors="replace")})
            assert proc.returncode == 0, "could not hash pre-existing /data storage read-only"
            hashes = {}
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:*") as archive:
                for member in archive:
                    if member.isfile() and _SQLITE_NAME.fullmatch(Path(member.name).name):
                        stream = archive.extractfile(member)
                        assert stream is not None
                        hashes[member.name] = hashlib.file_digest(stream, "sha256").hexdigest()
            result[cid] = hashes
        return result

    def snapshot_preservation(self) -> dict:
        initial = self.inventory()
        # Desktop Resource Saver can answer initial inventory requests from its
        # sleeping-VM cache. Read actual owned-by-user /data bytes first, without
        # starting any container, then establish the preservation baseline from
        # the awake Engine. A host without /data mounts uses read-only system df.
        if any(m.get("Destination") == "/data"
               for data in initial["containers"].values() for m in data["mounts"]):
            self._container_sqlite(initial)
        else:
            wake = self.run(["docker", "system", "df", "--format", "json"], timeout=120)
            assert wake.returncode == 0, "read-only Docker Engine wake failed"
        inventory = self.inventory()
        container_sqlite = self._container_sqlite(inventory)
        stable = self.inventory()
        assert inventory == stable, (
            "Docker inventory changed during read-only preservation snapshot; "
            "no stable baseline established")
        # Git common-dir locates the original checkout without guessing its path.
        common = self.run(["git", "rev-parse", "--git-common-dir"], timeout=30)
        assert common.returncode == 0
        common_path = Path(common.stdout.strip())
        if not common_path.is_absolute():
            common_path = (self.root / common_path).resolve()
        host = {}
        for checkout in {self.root, common_path.parent}:
            data_dir = checkout / "data"
            if data_dir.is_dir():
                for path in sorted(data_dir.iterdir()):
                    if path.is_file() and _SQLITE_NAME.fullmatch(path.name):
                        host[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"docker": inventory, "host_sqlite": host,
                "container_sqlite": container_sqlite}

    def assert_preserved(self, before: dict) -> dict:
        after = self.snapshot_preservation()
        (self.out / "preservation-after.json").write_text(
            json.dumps(after, indent=2), encoding="utf-8")
        changed = [key for key in before if before[key] != after.get(key)]
        assert not changed, f"pre-existing resources changed: {changed}; inspect preservation files"
        return {"unchanged": True, "docker_counts": {
            key: len(value) for key, value in after["docker"].items()},
            "host_sqlite_count": len(after["host_sqlite"]),
            "container_sqlite_count": sum(len(x) for x in after["container_sqlite"].values())}
