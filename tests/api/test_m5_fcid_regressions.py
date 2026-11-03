"""F-CID lifecycle regressions: canonical IDs and attached host storage."""

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def module():
    path = Path(__file__).parents[1] / "integration/test_m5_docker_e2e.py"
    spec = importlib.util.spec_from_file_location("fcid_regressions", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def seed(run):
    with sqlite3.connect(run.bind_dir / "trustflow.db") as db:
        db.execute("CREATE TABLE attached_sentinel(v TEXT)")
        db.execute("INSERT INTO attached_sentinel VALUES ('preserve while attached')")
    db.close()
    for suffix in ("-wal", "-shm", "-journal"):
        (run.bind_dir / ("trustflow.db" + suffix)).write_bytes(b"FCID_SIDECAR_SENTINEL")


def hashes(run):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in run.bind_dir.iterdir() if p.is_file()}


def reply(args, output="", rc=0, error=""):
    return subprocess.CompletedProcess(args, rc, output, error)


@pytest.mark.parametrize("mode", [
    "prefix", "full", "empty", "too-short", "nonhex", "wrong-full",
    "inspect-failed", "timeout", "permission", "malformed", "ambiguous",
    "wrong-service", "wrong-run", "legacy-prefix",
])
def test_full_lifecycle_preserves_attached_storage(module, tmp_path, monkeypatch, mode):
    m = module
    ids = {"backend": "c" * 64, "frontend": "d" * 64}
    images = {"backend": "sha256:" + "a" * 64, "frontend": "sha256:" + "b" * 64}
    state = {"run": None, "alive": {}, "images": set(), "calls": []}
    init = m._StackRun.__init__

    def capture(obj, *args, **kwargs):
        init(obj, *args, **kwargs)
        state["run"] = obj

    monkeypatch.setattr(m._StackRun, "__init__", capture)

    def metadata(cid):
        run = state["run"]
        service = next(k for k, value in ids.items() if value == cid)
        return {"Id": cid, "Name": "/" + service, "Image": images[service],
                "Config": {"Labels": {m.PROJECT_LABEL: run.project,
                                      m.RUN_LABEL: run.run_id,
                                      "com.docker.compose.service": service}},
                "State": {"Running": False}, "Mounts": [
                    {"Type": "bind", "Source": run.bind_dir.as_posix(),
                     "Destination": "/data"}] if service == "backend" else []}

    def boundary(args, **kwargs):
        state["calls"].append(list(args))
        run = state["run"]
        labels = {m.PROJECT_LABEL: run.project, m.RUN_LABEL: run.run_id}
        if args[:2] == ["docker", "compose"]:
            if "build" in args:
                state["images"] = set(images.values())
            if "config" in args:
                return reply(args, json.dumps({"services": {"backend": {"volumes": [
                    {"type": "bind", "source": run.bind_dir.as_posix(), "target": "/data"}
                ]}}}))
            if "create" in args:
                state["alive"] = dict(ids)
                seed(run)
                state["before"] = hashes(run)
            if "ps" in args:
                cid = ids[args[-1]]
                query = {"empty": "", "too-short": cid[:11], "nonhex": "g" * 12,
                         "wrong-full": "e" * 64}.get(mode, cid if mode == "full" else cid[:12])
                return reply(args, query)
            if "start" in args:
                if mode == "legacy-prefix":
                    run.container_ids["backend"] = ids["backend"][:12]
                return reply(args, rc=1, error="PRIMARY_FCID_SETUP_FAILURE")
            return reply(args)
        if args[:2] == ["docker", "ps"] or args[:3] == ["docker", "container", "ls"]:
            return reply(args, "\n".join(state["alive"].values()))
        if args[:3] == ["docker", "container", "inspect"]:
            if mode == "inspect-failed":
                return reply(args, rc=1, error="FCID_INSPECT_DENIED")
            if mode == "timeout":
                raise subprocess.TimeoutExpired(args, 120)
            if mode == "permission":
                raise PermissionError("FCID_PERMISSION")
            if mode == "malformed":
                return reply(args, "{broken")
            cid = next((value for value in ids.values() if value.startswith(args[-1])),
                       ids["backend"])
            raw = metadata(cid)
            if mode == "wrong-service":
                raw["Config"]["Labels"]["com.docker.compose.service"] = "other"
            if mode == "wrong-run":
                raw["Config"]["Labels"][m.RUN_LABEL] = "other-run"
            return reply(args, json.dumps([raw, raw] if mode == "ambiguous" else [raw]))
        if args[:3] == ["docker", "image", "ls"]:
            return reply(args, "\n".join(sorted(state["images"])))
        if args[:3] == ["docker", "image", "inspect"]:
            return reply(args, json.dumps([{"Id": args[-1], "Config": {"Labels": labels}}]))
        if args[:2] == ["docker", "inspect"]:
            if args[-1] in images.values():
                return reply(args, json.dumps([{"Id": args[-1], "Config": {"Labels": labels}}]))
            cid = next(value for value in ids.values() if value.startswith(args[-1]))
            return reply(args, json.dumps([metadata(cid)]))
        if args[:2] == ["docker", "rm"]:
            return reply(args, rc=1, error="FCID_ATTACHED_CONTAINER_REMOVAL_REFUSED")
        if args[:2] == ["docker", "rmi"]:
            return reply(args, rc=1, error="FCID_IMAGE_STILL_REFERENCED")
        return reply(args)

    monkeypatch.setattr(m, "_run", boundary)
    with pytest.raises(BaseException) as caught:
        m._run_stack_test(tmp_path)
    run = state["run"]
    assert run.workspace.exists(), "attached container lost its host workspace"
    assert hashes(run) == state["before"]
    assert state["alive"] == ids
    graph = m._exception_graph_text(caught.value)
    assert "workspace-cleanup" in graph
    assert run.run_id in graph
    if mode in ("prefix", "full"):
        assert run.container_ids == ids
        assert "PRIMARY_FCID_SETUP_FAILURE" in graph
    elif mode == "legacy-prefix":
        assert "PRIMARY_FCID_SETUP_FAILURE" in graph
    else:
        assert "container-identity" in graph
        assert "backend" not in run.container_ids


@pytest.mark.parametrize("recorded", ["c" * 12, "", "c" * 11, "g" * 64, "e" * 64])
def test_cleanup_refuses_unresolved_or_other_attached_container(
        module, tmp_path, monkeypatch, recorded):
    run = module._StackRun(tmp_path)
    run._prepare_workspace()
    seed(run)
    before = hashes(run)
    run.container_ids = {"backend": recorded}

    def boundary(args, **kwargs):
        if args[:3] == ["docker", "container", "ls"]:
            return reply(args, "c" * 64)
        return reply(args, json.dumps([{"Id": "c" * 64, "Mounts": [
            {"Type": "bind", "Source": run.bind_dir.as_posix(), "Destination": "/data"}
        ], "State": {"Running": False}}]))

    monkeypatch.setattr(module, "_run", boundary)
    log = module._FailureLog(run.run_id)
    run._cleanup_workspace(log.add)
    assert run.workspace.exists() and hashes(run) == before
    assert log.items and isinstance(log.items[0], module.CleanupFailure)
    assert log.items[0].preserved and log.items[0].run_id == run.run_id


@pytest.mark.parametrize("inventory", ["", "d" * 64])
def test_canonical_absence_allows_safe_cleanup(module, tmp_path, monkeypatch, inventory):
    run = module._StackRun(tmp_path)
    run._prepare_workspace()
    seed(run)
    run.container_ids = {"backend": "c" * 64}

    def boundary(args, **kwargs):
        if args[:3] == ["docker", "container", "ls"]:
            return reply(args, inventory)
        return reply(args, json.dumps([{"Id": "d" * 64, "Mounts": []}]))

    monkeypatch.setattr(module, "_run", boundary)
    log = module._FailureLog(run.run_id)
    run._cleanup_workspace(log.add)
    assert not log.items and not run.workspace.exists()


@pytest.mark.parametrize("fault", [
    "permission", "timeout", "nonzero", "malformed", "wrong-id",
    "missing-mounts", "bad-mount", "unknown-type", "missing-source",
])
def test_unknown_mount_inspection_preserves_storage(module, tmp_path, monkeypatch, fault):
    run = module._StackRun(tmp_path)
    run._prepare_workspace()
    seed(run)
    before = hashes(run)
    run.container_ids = {"backend": "c" * 64}

    def boundary(args, **kwargs):
        if args[:3] == ["docker", "container", "ls"]:
            return reply(args, "d" * 64)
        if fault == "permission":
            raise PermissionError("FCID_MOUNT_PERMISSION")
        if fault == "timeout":
            raise subprocess.TimeoutExpired(args, 120)
        if fault == "nonzero":
            return reply(args, rc=1, error="FCID_MOUNT_INSPECT_FAILED")
        if fault == "malformed":
            return reply(args, "{bad")
        raw = {"Id": "d" * 64, "Mounts": []}
        if fault == "wrong-id":
            raw["Id"] = "e" * 64
        elif fault == "missing-mounts":
            del raw["Mounts"]
        else:
            raw["Mounts"] = {"bad-mount": [None], "unknown-type": [{"Type": "unknown"}],
                             "missing-source": [{"Type": "bind"}]}[fault]
        return reply(args, json.dumps([raw]))

    monkeypatch.setattr(module, "_run", boundary)
    log = module._FailureLog(run.run_id)
    run._cleanup_workspace(log.add)
    assert run.workspace.exists() and hashes(run) == before
    assert log.items and log.items[0].preserved
    assert log.items[0].image_id == "d" * 64
    assert log.items[0].command == "docker inspect " + "d" * 64
