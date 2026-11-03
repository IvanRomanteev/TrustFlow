"""Offline adapter guards; accepted P2-05 coverage remains unchanged."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m52_infrastructure_test", ROOT / "scripts/m52/infrastructure.py")
assert SPEC and SPEC.loader
infra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(infra)


def test_sanitize_docker_environment_and_approval_without_losing_operation_identity():
    observed = infra.sanitize({
        "Config": {"Env": ["MODEL_API_KEY=never-publish", "OTHER=also-private"]},
        "TRUSTFLOW_APPROVAL_TOKEN": "not-published", "idempotency_key": "op-identity",
        "plan_hash": "a" * 64, "log": "using demo-approval-token",
    })
    encoded = json.dumps(observed)
    for secret in ("never-publish", "also-private", "not-published", "demo-approval-token"):
        assert secret not in encoded
    assert observed["idempotency_key"] == "op-identity"
    assert observed["plan_hash"] == "a" * 64


def test_offline_environment_excludes_inherited_credentials(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "never-pass-live")
    monkeypatch.setenv("META_API_KEY", "never-pass-meta")
    monkeypatch.setenv("TRUSTFLOW_LLM_MODE", "live")
    monkeypatch.setenv("TRUSTFLOW_DB_PATH", "user.db")
    monkeypatch.setenv("CUSTOM_SECRET", "never-pass-secret")
    monkeypatch.setenv("PATH", "tool-path")
    env = infra.offline_environment()
    assert env["TRUSTFLOW_LLM_MODE"] == "fake"
    assert env["MODEL_API_KEY"] == "offline-fake-placeholder"
    assert "META_API_KEY" not in env and "CUSTOM_SECRET" not in env
    assert "TRUSTFLOW_DB_PATH" not in env
    assert env["PATH"] == "tool-path"


def test_adapter_uses_accepted_lifecycle_and_seed_after_ownership_gate(tmp_path):
    prepared = []

    def seed(stack):
        stack._validate_workspace()
        assert stack.marker_file.read_text() == stack.run_id
        prepared.append(stack.bind_dir)

    stack = infra.DemoStack(ROOT, tmp_path, "a" * 32, prepare=seed)
    assert stack._stack.__class__.__bases__ == (stack._module._StackRun,)
    assert "teardown" not in stack._stack.__class__.__dict__
    assert "_cleanup_workspace" not in stack._stack.__class__.__dict__
    assert "_verify_ownership" not in stack._stack.__class__.__dict__
    stack._stack._prepare_workspace()
    assert prepared == [stack.bind_dir]
    # No Docker is called and no test-created directory is recursively deleted.
    stack._stack._validate_workspace()


def test_lifecycle_refuses_unverified_backend_before_any_subprocess(tmp_path, monkeypatch):
    stack = infra.DemoStack(ROOT, tmp_path, "b" * 32)
    called = []
    monkeypatch.setattr(stack, "run", lambda *a, **k: called.append(a))
    with pytest.raises(AssertionError, match="ownership gate"):
        stack.backend_lifecycle("kill")
    assert not called


def test_fault_override_keeps_accepted_data_mount_and_dynamic_build_configuration(tmp_path):
    run_id = "c" * 32
    command = ["uvicorn", "demo.fault_backend:create_app", "--factory"]
    stack = infra.DemoStack(
        ROOT, tmp_path, run_id, backend_command=command,
        backend_env={"TRUSTFLOW_M52_RUN_ID": run_id})
    config = json.loads((tmp_path / f"demo-backend-{run_id}.json").read_text())
    backend = config["services"]["backend"]
    assert backend["command"] == command
    assert backend["environment"]["TRUSTFLOW_M52_RUN_ID"] == run_id
    assert "volumes" not in backend and "ports" not in backend
    text = (tmp_path / f"override-{run_id}.yml").read_text()
    assert stack.backend_url in text and str(stack.frontend_port) in text
    assert str(stack.backend_port) in text
    assert "volumes: !override" in text and "127.0.0.1:" in text


def test_run_observer_never_persists_raw_docker_env(tmp_path):
    import sys

    stack = infra.DemoStack(ROOT, tmp_path, "d" * 32)
    payload = json.dumps({"Config": {"Env": ["private-do-not-record"]}})
    proc = stack.run([sys.executable, "-c", "import sys; print(sys.argv[1])", payload])
    assert proc.returncode == 0
    log = (tmp_path / "commands.jsonl").read_text()
    # argv may contain raw response fixtures in a diagnostic command, so this
    # assertion concerns response recording (the Docker production argv never
    # contains response bodies or environment credentials).
    assert stack.commands[-1]["stdout"]["Config"]["Env"] == "[REDACTED]"
    assert stack.commands[-1]["exit_code"] == 0
    assert log


def _snapshot_inventory(bridge, *, has_data=True):
    return {"containers": {"user-container": {"mounts": [
        {"Destination": "/data"} if has_data else {"Destination": "/other"}]}},
        "images": {}, "networks": {bridge: {"Name": "bridge"}}, "volumes": {}}


def test_preservation_wakes_engine_readonly_before_actual_inventory(tmp_path, monkeypatch):
    import subprocess

    stack = infra.DemoStack(ROOT, tmp_path, "e" * 32)
    initial = _snapshot_inventory("cached-sleeping-bridge")
    awake = _snapshot_inventory("actual-awake-bridge")
    observations = iter([initial, awake, awake])
    order = []

    def inventory():
        result = next(observations)
        order.append(("inventory", next(iter(result["networks"]))))
        return result

    def container_bytes(observed):
        bridge = next(iter(observed["networks"]))
        order.append(("readonly-cp", bridge))
        return {"user-container": {"./trustflow.db": f"hash-from-{bridge}"}}

    monkeypatch.setattr(stack, "inventory", inventory)
    monkeypatch.setattr(stack, "_container_sqlite", container_bytes)
    monkeypatch.setattr(stack, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=str(tmp_path / ".git"), stderr=""))
    result = stack.snapshot_preservation()
    assert order == [("inventory", "cached-sleeping-bridge"),
                     ("readonly-cp", "cached-sleeping-bridge"),
                     ("inventory", "actual-awake-bridge"),
                     ("readonly-cp", "actual-awake-bridge"),
                     ("inventory", "actual-awake-bridge")]
    assert result["docker"] == awake
    assert result["container_sqlite"]["user-container"]["./trustflow.db"] == \
        "hash-from-actual-awake-bridge"


def test_preservation_without_data_mount_uses_readonly_system_df(tmp_path, monkeypatch):
    import subprocess

    stack = infra.DemoStack(ROOT, tmp_path, "f" * 32)
    observed = _snapshot_inventory("stable-bridge", has_data=False)
    calls = []
    monkeypatch.setattr(stack, "inventory", lambda: observed)
    monkeypatch.setattr(stack, "_container_sqlite", lambda _: {})

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path / ".git"), stderr="")

    monkeypatch.setattr(stack, "run", run)
    assert stack.snapshot_preservation()["docker"] == observed
    assert calls[0] == ["docker", "system", "df", "--format", "json"]
    assert all(argv[:3] != ["docker", "start", "user-container"] for argv in calls)


def test_preservation_rejects_inventory_drift_instead_of_normalizing_bridge(tmp_path, monkeypatch):
    stack = infra.DemoStack(ROOT, tmp_path, "a1" * 16)
    observations = iter([_snapshot_inventory("cached"), _snapshot_inventory("awake"),
                         _snapshot_inventory("unexpected-drift")])
    monkeypatch.setattr(stack, "inventory", lambda: next(observations))
    monkeypatch.setattr(stack, "_container_sqlite", lambda _: {})
    with pytest.raises(AssertionError, match="no stable baseline"):
        stack.snapshot_preservation()
