"""Independent offline checks of M5.2 evidence, seed and publication boundaries."""

import copy
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.m52 import evidence, ledger  # noqa: E402
from scripts.m52.infrastructure import DemoStack  # noqa: E402


def _owned_stack(tmp_path):
    stack = DemoStack(ROOT, tmp_path, "e" * 32)._stack
    stack._prepare_workspace()
    return stack


def test_seed_refuses_existing_database_without_changing_bytes(tmp_path):
    stack = _owned_stack(tmp_path)
    database = stack.bind_dir / "trustflow.db"
    database.write_bytes(b"foreign-existing-sqlite-content")
    before = database.read_bytes()
    with pytest.raises(AssertionError, match="existing SQLite"):
        ledger.seed_owned(stack)
    assert database.read_bytes() == before


def test_seed_refuses_unrecorded_workspace_before_creating_database(tmp_path):
    stack = DemoStack(ROOT, tmp_path, "f" * 32)._stack
    stack.bind_dir.mkdir(parents=True)
    stack.marker_file.write_text(stack.run_id)
    with pytest.raises(AssertionError, match="identity was not recorded"):
        ledger.seed_owned(stack)
    assert set(stack.bind_dir.iterdir()) == {stack.marker_file}


def test_seed_refuses_foreign_sidecar_in_owned_directory(tmp_path):
    stack = _owned_stack(tmp_path)
    foreign = stack.bind_dir / "trustflow.db-wal"
    foreign.write_bytes(b"foreign-wal-never-reset")
    with pytest.raises(AssertionError, match="foreign storage"):
        ledger.seed_owned(stack)
    assert foreign.read_bytes() == b"foreign-wal-never-reset"
    assert not (stack.bind_dir / "trustflow.db").exists()


def test_seed_validates_marker_identity_before_opening_sqlite(tmp_path):
    stack = _owned_stack(tmp_path)
    stack.marker_file.write_text("different-owner")
    with pytest.raises(AssertionError, match="marker changed"):
        ledger.seed_owned(stack)
    assert not (stack.bind_dir / "trustflow.db").exists()


def test_fresh_seed_has_known_balances_and_empty_history(tmp_path):
    stack = _owned_stack(tmp_path)
    ledger.seed_owned(stack)
    result = ledger.snapshot(stack.bind_dir / "trustflow.db")
    assert result["balances"] == ledger.BALANCES
    assert result["total"] == 36200.0
    assert not result["transactions"] and not result["runs"] and not result["audit"]
    ledger.assert_integrity(result)


def test_readonly_snapshot_reads_committed_live_wal_and_excludes_uncommitted_data(tmp_path):
    stack = _owned_stack(tmp_path)
    ledger.seed_owned(stack)
    path = stack.bind_dir / "trustflow.db"
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        before_main = hashlib.sha256(path.read_bytes()).hexdigest()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE ledger_accounts SET balance=25010 WHERE account_id='acc_alice'")
        writer.execute("UPDATE ledger_accounts SET balance=1190 WHERE account_id='acc_bob'")
        writer.execute("COMMIT")
        assert Path(str(path) + "-wal").stat().st_size > 0
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before_main
        wal_before = Path(str(path) + "-wal").read_bytes()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE ledger_accounts SET balance=99999 WHERE account_id='acc_alice'")
        result = ledger.snapshot(path)
        assert result["balances"]["acc_alice"] == 25010.0
        assert result["balances"]["acc_bob"] == 1190.0
        assert result["total"] == ledger.TOTAL
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before_main
        assert Path(str(path) + "-wal").read_bytes() == wal_before
        writer.execute("ROLLBACK")
    finally:
        writer.close()


def test_snapshot_closes_readonly_connection_before_return(tmp_path, monkeypatch):
    stack = _owned_stack(tmp_path)
    ledger.seed_owned(stack)
    real_connect = ledger.sqlite3.connect
    observed = []

    def capture(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        observed.append(connection)
        return connection

    monkeypatch.setattr(ledger.sqlite3, "connect", capture)
    ledger.snapshot(stack.bind_dir / "trustflow.db")
    assert len(observed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed[0].execute("SELECT 1")


def _projection_input():
    scenarios = [{"id": f"S{n}", "verdict": "PASS", "assertions": [
        {"name": f"safety_{n}", "passed": True}]} for n in range(1, 7)]
    scenarios[1]["concurrent_statuses"] = [200, 409]
    run = {"run_id": "volatile-run-one", "goal": {"title": "Transfer 5000"},
           "status": "COMPLETED", "verification_attempts": 1,
           "approvals": [{"created_at": "volatile-timestamp", "plan_hash": "a" * 64}],
           "verification": {"checks": [
               {"name": "intent_coverage:volatile-intent-one", "passed": True,
                "detail": "volatile-intent-one"}]}}
    final = {"runs": {run["run_id"]: {"run": run}},
             "balances": {"acc_alice": 20000.0, "acc_bob": 6200.0, "acc_ops": 10000.0},
             "total": ledger.TOTAL,
             "transactions": [{"tx_id": "tx_000001", "from_account": "acc_alice",
                               "to_account": "acc_bob", "amount": 5000.0, "currency": "USD",
                               "idempotency_key": "volatile-intent-one", "seq": 1}],
             "audit": [{"run_id": run["run_id"], "action": "approval_granted",
                        "ts": "volatile-timestamp", "payload_hash": "a" * 64}]}
    browser = {"chromium_version": "one-version", "assertions": [
        {"name": "real_browser_approval", "passed": True}],
        "screenshots": ["screenshots/S1.png"], "events": [{"url": "volatile-port"}]}
    return scenarios, final, browser, {"passed": True}, {"unchanged": True}


def test_normalization_ignores_documented_volatile_fields():
    left = _projection_input()
    right = copy.deepcopy(left)
    run = right[1]["runs"].pop("volatile-run-one")
    run["run"]["run_id"] = "volatile-run-two"
    run["run"]["approvals"][0].update(created_at="new-timestamp", plan_hash="b" * 64)
    run["run"]["verification"]["checks"][0].update(
        name="intent_coverage:volatile-intent-two", detail="different-hash")
    right[1]["runs"]["volatile-run-two"] = run
    right[1]["audit"][0].update(run_id="volatile-run-two", ts="new-ts", payload_hash="b" * 64)
    right[1]["transactions"][0]["idempotency_key"] = "volatile-intent-two"
    right[2].update(chromium_version="new-version", events=[{"url": "another-port"}])
    assert evidence.normalized(*left) == evidence.normalized(*right)


@pytest.mark.parametrize("changed", [
    "verdict", "assertion", "balance", "transfer", "check", "status", "cleanup",
    "preservation", "browser", "concurrent_statuses",
])
def test_normalization_retains_safety_relevant_differences(changed):
    left = _projection_input()
    right = copy.deepcopy(left)
    if changed == "verdict":
        right[0][0]["verdict"] = "FAIL"
    elif changed == "assertion":
        right[0][0]["assertions"][0]["passed"] = False
    elif changed == "balance":
        right[1]["balances"]["acc_alice"] += 1
    elif changed == "transfer":
        right[1]["transactions"][0]["amount"] += 1
    elif changed == "check":
        right[1]["runs"]["volatile-run-one"]["run"]["verification"]["checks"][0]["passed"] = False
    elif changed == "status":
        right[1]["runs"]["volatile-run-one"]["run"]["status"] = "FAILED"
    elif changed == "cleanup":
        right[3]["passed"] = False
    elif changed == "preservation":
        right[4]["unchanged"] = False
    elif changed == "browser":
        right[2]["assertions"][0]["passed"] = False
    elif changed == "concurrent_statuses":
        right[0][1]["concurrent_statuses"] = [200, 200]
    assert evidence.digest(evidence.normalized(*left)) != evidence.digest(
        evidence.normalized(*right))


def _publication_fixture(out):
    inputs = _projection_input()
    projection = evidence.normalized(*inputs)
    manifest = {"scenarios": inputs[0], "cleanup": inputs[3], "preservation": inputs[4]}
    required = ["scenario-results.json", "ledger-before.json", "ledger-after.json",
                "audit-summary.json", "commands.jsonl", "environment.json", "runtime-logs.json",
                "runtime-bindings.json", "preservation-before.json", "preservation-after.json"]
    for name in required:
        evidence.write_json(out / name, {})
    (out / "README.md").write_text("Test-owned publication fixture.")
    evidence.write_json(out / "browser-log.json", inputs[2])
    (out / "screenshots").mkdir()
    # Publication hashes bytes; image content is verified by the real Chromium
    # runner. This offline fixture checks file presence and evidence consistency.
    (out / "screenshots/S1.png").write_bytes(b"test-owned-screenshot-bytes")
    return manifest, projection


def test_publish_partial_attempt_fails_without_complete_and_preserves_previous_success(tmp_path):
    success = evidence.new_attempt(tmp_path, "success")
    manifest, projection = _publication_fixture(success)
    assert evidence.publish(success, manifest, [], projection)
    before = {p.relative_to(success): p.read_bytes() for p in success.rglob("*") if p.is_file()}
    failed = evidence.new_attempt(tmp_path, "later-failure")
    failed_manifest, failed_projection = _publication_fixture(failed)
    (failed / "ledger-after.json").unlink()
    assert not evidence.publish(failed, failed_manifest, [], failed_projection)
    assert not (failed / "COMPLETE.json").exists()
    failure = json.loads((failed / "FAILED_ATTEMPT.json").read_text())
    assert failure["verdict"] == "FAIL"
    assert any("ledger-after.json" in message for message in failure["failures"])
    assert before == {p.relative_to(success): p.read_bytes()
                      for p in success.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        evidence.new_attempt(tmp_path, "success")


@pytest.mark.parametrize("violation", [
    "duplicate_scenario", "false_scenario_assertion", "false_browser_assertion",
    "missing_referenced_screenshot", "cleanup_failure", "preservation_failure",
])
def test_publish_refuses_false_positive_complete(tmp_path, violation):
    out = evidence.new_attempt(tmp_path, violation)
    manifest, projection = _publication_fixture(out)
    if violation == "duplicate_scenario":
        manifest["scenarios"][-1]["id"] = "S1"
    elif violation == "false_scenario_assertion":
        manifest["scenarios"][0]["assertions"][0]["passed"] = False
    elif violation == "false_browser_assertion":
        log = json.loads((out / "browser-log.json").read_text())
        log["assertions"][0]["passed"] = False
        evidence.write_json(out / "browser-log.json", log)
    elif violation == "missing_referenced_screenshot":
        log = json.loads((out / "browser-log.json").read_text())
        log["screenshots"].append("screenshots/absent.png")
        evidence.write_json(out / "browser-log.json", log)
    elif violation == "cleanup_failure":
        manifest["cleanup"]["passed"] = False
    elif violation == "preservation_failure":
        manifest["preservation"]["unchanged"] = False
    failures = []
    assert not evidence.publish(out, manifest, failures, projection)
    assert failures and (out / "FAILED_ATTEMPT.json").is_file()
    assert not (out / "COMPLETE.json").exists()


def test_compare_detects_prior_projection_tampering(tmp_path):
    out = evidence.new_attempt(tmp_path, "prior")
    manifest, projection = _publication_fixture(out)
    assert evidence.publish(out, manifest, [], projection)
    assert evidence.compare(out, projection)["equivalent"]
    evidence.write_json(out / "normalized-results.json", {"changed": True})
    with pytest.raises(AssertionError, match="digest is invalid"):
        evidence.compare(out, projection)


def test_atomic_json_failure_preserves_old_file_and_own_temp_only(tmp_path, monkeypatch):
    path = tmp_path / "existing.json"
    path.write_bytes(b"original-evidence")
    sentinel = tmp_path / ".unrelated.tmp"
    sentinel.write_bytes(b"unrelated-do-not-remove")

    def unavailable_replace(*args):
        raise PermissionError("simulated atomic rename refusal")

    monkeypatch.setattr(evidence.os, "replace", unavailable_replace)
    with pytest.raises(PermissionError, match="rename refusal"):
        evidence.write_json(path, {"new": "attempt"})
    assert path.read_bytes() == b"original-evidence"
    assert sentinel.read_bytes() == b"unrelated-do-not-remove"
    assert set(tmp_path.iterdir()) == {path, sentinel}


@pytest.mark.parametrize("late_failure", [
    "malformed_browser_log", "readme_write", "artifact_read", "manifest_write", "complete_write",
])
def test_late_publication_failure_retains_failed_marker(tmp_path, monkeypatch, late_failure):
    out = evidence.new_attempt(tmp_path, late_failure)
    manifest, projection = _publication_fixture(out)
    original_write_json, original_write_text = evidence.write_json, evidence.write_text
    original_read_bytes = Path.read_bytes
    if late_failure == "malformed_browser_log":
        (out / "browser-log.json").write_text("{broken-json")
    elif late_failure == "readme_write":
        def refused_readme(path, text):
            if path.name == "README.md":
                raise PermissionError("demo-approval-token: simulated README write refusal")
            return original_write_text(path, text)

        monkeypatch.setattr(evidence, "write_text", refused_readme)
    elif late_failure == "artifact_read":
        def refused_artifact(path):
            if path.name == "ledger-after.json":
                raise PermissionError("demo-approval-token: simulated artifact read refusal")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", refused_artifact)
    else:
        refused_name = "manifest.json" if late_failure == "manifest_write" else "COMPLETE.json"

        def refused_write(path, value):
            if path.name == refused_name:
                raise PermissionError("demo-approval-token: simulated final write refusal")
            return original_write_json(path, value)

        monkeypatch.setattr(evidence, "write_json", refused_write)
    failures = []
    if late_failure in {"malformed_browser_log", "readme_write"}:
        passed = evidence.finalize(out, manifest, failures, _projection_input()[1], "replay")
    else:
        passed = evidence.publish(out, manifest, failures, projection)
    assert passed is False
    assert not (out / "COMPLETE.json").exists()
    marker_text = (out / "FAILED_ATTEMPT.json").read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert marker["verdict"] == "FAIL" and marker["failures"]
    assert "demo-approval-token" not in marker_text
    assert not list(out.glob("*.tmp"))


def test_finalization_refuses_to_modify_completed_invocation(tmp_path):
    out = evidence.new_attempt(tmp_path, "completed")
    manifest, projection = _publication_fixture(out)
    assert evidence.publish(out, manifest, [], projection)
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    failures = []
    assert not evidence.finalize(out, manifest, failures, {}, "replacement README")
    assert failures
    assert before == {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
