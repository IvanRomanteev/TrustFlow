"""Behavioral regressions from the independent R7 BLOCKED review."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def module():
    path = Path(__file__).parents[1] / "integration/test_m5_docker_e2e.py"
    spec = importlib.util.spec_from_file_location("r7_regressions", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def reply(args, data="", rc=0, error=""):
    return subprocess.CompletedProcess(args, rc, data, error)


def test_replaced_workspace_preserved_by_full_entrypoint(module, tmp_path, monkeypatch):
    """Replacement before create completes must never reach application start."""
    m = module
    state = {"run": None, "built": False, "containers": {}, "started": False}
    images = {"backend": "sha256:" + "a" * 64, "frontend": "sha256:" + "b" * 64}
    cids = {"backend": "c" * 64, "frontend": "d" * 64}
    init = m._StackRun.__init__

    def capture(obj, *args, **kwargs):
        init(obj, *args, **kwargs)
        state["run"] = obj

    monkeypatch.setattr(m._StackRun, "__init__", capture)

    def boundary(args, **kwargs):
        run = state["run"]
        labels = {m.PROJECT_LABEL: run.project, m.RUN_LABEL: run.run_id}
        if args[:2] == ["docker", "compose"]:
            if "build" in args:
                state["built"] = True
            if "config" in args:
                return reply(args, json.dumps({"services": {"backend": {"volumes": [
                    {"type": "bind", "source": run.bind_dir.as_posix(), "target": "/data"}
                ]}}}))
            if "create" in args:
                state["containers"] = dict(cids)
                run.workspace.rename(tmp_path / "original")
                run.bind_dir.mkdir(parents=True)
                run.marker_file.write_text(run.run_id)
                (run.bind_dir / "foreign.txt").write_text("keep me")
            if "ps" in args:
                return reply(args, cids[args[-1]])
            if "start" in args:
                state["started"] = True
                return reply(args, rc=1, error="UNSAFE_START_MARKER")
            return reply(args)
        if args[:2] == ["docker", "ps"] or args[:3] == ["docker", "container", "ls"]:
            return reply(args, "\n".join(state["containers"].values()))
        if args[:3] == ["docker", "image", "ls"]:
            return reply(args, "\n".join(images.values())
                         if state["built"] and "--filter" in args else "")
        if args[:3] == ["docker", "image", "inspect"]:
            return reply(args, json.dumps([{"Id": args[-1], "Config": {"Labels": labels}}]))
        if args[:2] == ["docker", "inspect"]:
            service = next(k for k, v in cids.items() if v == args[-1])
            if "-f" in args:
                return reply(args, rc=1, error="No such object")
            return reply(args, json.dumps([{
                "Id": cids[service], "Name": "/" + service,
                "Image": images[service], "Config": {"Labels": labels},
                "State": {"Running": False}, "Mounts": [
                    {"Type": "bind", "Source": run.bind_dir.as_posix(), "Destination": "/data"}
                ] if service == "backend" else []}]))
        if args[:2] == ["docker", "rm"]:
            state["containers"] = {k: v for k, v in state["containers"].items()
                                   if v != args[-1]}
        if args[:2] == ["docker", "rmi"]:
            state["built"] = False
        return reply(args)

    monkeypatch.setattr(m, "_run", boundary)
    with pytest.raises((AssertionError, ExceptionGroup)):
        m._run_stack_test(tmp_path)
    assert not state["started"], "replacement storage reached application start"
    assert (state["run"].bind_dir / "foreign.txt").read_text() == "keep me"


@pytest.mark.parametrize("replacement", ["workspace", "data", "marker"])
def test_cleanup_rejects_replaced_filesystem_identity(module, tmp_path, monkeypatch, replacement):
    run = module._StackRun(tmp_path)
    run._prepare_workspace()
    if replacement == "marker":
        run.marker_file.rename(tmp_path / "original-marker")
        run.marker_file.write_text(run.run_id)
    else:
        target = run.workspace if replacement == "workspace" else run.bind_dir
        target.rename(tmp_path / "original")
        run.bind_dir.mkdir(parents=True, exist_ok=True)
        run.marker_file.write_text(run.run_id)
    sentinel = run.bind_dir / "trustflow.db"
    sentinel.write_bytes(b"foreign data must survive")
    monkeypatch.setattr(module, "_run", lambda args, **kw: reply(args))
    failures = []
    run._cleanup_workspace(lambda *args: failures.append(args))
    assert sentinel.read_bytes() == b"foreign data must survive"
    assert failures


@pytest.mark.parametrize("response", ["permission", "malformed", "timeout"])
def test_uncertain_container_absence_preserves_workspace(module, tmp_path, monkeypatch, response):
    run = module._StackRun(tmp_path)
    run._prepare_workspace()
    run.container_ids = {"backend": "c" * 64}
    sentinel = run.bind_dir / "trustflow.db"
    sentinel.write_bytes(b"mounted data")

    def boundary(args, **kwargs):
        if response == "timeout":
            raise subprocess.TimeoutExpired(args, 120)
        if response == "permission":
            return reply(args, rc=1, error="INSPECT_PERMISSION_MARKER")
        if "inspect" in args:
            return reply(args, rc=1, error="No such object")
        return reply(args, "MALFORMED_CONTAINER_LIST")

    monkeypatch.setattr(module, "_run", boundary)
    failures = []
    run._cleanup_workspace(lambda *args: failures.append(args))
    assert sentinel.read_bytes() == b"mounted data"
    assert failures
    if response == "permission":
        assert "INSPECT_PERMISSION_MARKER" in str(failures)


def test_cleanup_preserves_unknown_workspace_contents(module, tmp_path, monkeypatch):
    run = module._StackRun(tmp_path)
    run._prepare_workspace()
    sentinel = run.bind_dir / "foreign.txt"
    sentinel.write_text("foreign content")
    monkeypatch.setattr(module, "_run", lambda args, **kw: reply(args))
    failures = []
    run._cleanup_workspace(lambda *args: failures.append(args))
    assert sentinel.read_text() == "foreign content"
    assert failures


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_ancestor_junction_redirection_preserved(module, tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    run = module._StackRun(parent)
    run._prepare_workspace()
    parent.rename(tmp_path / "original-parent")
    victim = tmp_path / "victim"
    (victim / run.workspace.name / "data").mkdir(parents=True)
    data = victim / run.workspace.name / "data"
    (data / run.marker_file.name).write_text(run.run_id)
    sentinel = data / "trustflow.db"
    sentinel.write_bytes(b"foreign junction target")
    # One shell, literal paths; all paths are children of this pytest tmp_path.
    made = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                           "New-Item -ItemType Junction -Path '" + str(parent).replace("'", "''")
                           + "' -Target '" + str(victim).replace("'", "''") + "' | Out-Null"],
                          capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    try:
        monkeypatch.setattr(module, "_run", lambda args, **kw: reply(args))
        failures = []
        run._cleanup_workspace(lambda *args: failures.append(args))
        assert sentinel.read_bytes() == b"foreign junction target"
        assert failures
    finally:
        # rmdir removes only the junction entry, never its target.
        os.rmdir(parent)


@pytest.mark.parametrize("defect", ["short-id", "wrong-id", "missing-name",
                                    "missing-state", "string-running", "missing-running"])
def test_incomplete_resource_metadata_is_unknown(module, tmp_path, monkeypatch, defect):
    run = module._StackRun(tmp_path)
    cid = "c" * 64
    raw = {"Id": cid, "Name": "/backend", "Config": {"Labels": {
        module.PROJECT_LABEL: run.project, module.RUN_LABEL: run.run_id}},
        "State": {"Running": False}}
    if defect == "short-id":
        raw["Id"] = "c"
    elif defect == "wrong-id":
        raw["Id"] = "d" * 64
    elif defect == "missing-name":
        del raw["Name"]
    elif defect == "missing-state":
        del raw["State"]
    elif defect == "string-running":
        raw["State"]["Running"] = "false"
    else:
        raw["State"] = {}

    def boundary(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return reply(args, cid + "\n")
        if args[:2] == ["docker", "inspect"]:
            return reply(args, json.dumps([raw]))
        return reply(args)

    monkeypatch.setattr(module, "_run", boundary)
    log = module._FailureLog(run.run_id)
    owned, foreign = run._discover(log)
    assert not owned["containers"] and not foreign
    assert log.items and "malformed" in str(log.items)
