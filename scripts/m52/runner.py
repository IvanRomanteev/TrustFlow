"""Six mandatory real-browser scenarios with independent SQLite evidence."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import shutil
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .browser import DemoBrowser
from .evidence import finalize, new_attempt, write_json
from .infrastructure import TOKEN, DemoStack, sanitize
from .ledger import (
    SEED_ID,
    assert_api_matches,
    assert_integrity,
    plan_hash,
    seed_owned,
    snapshot,
    transactions_for,
)
from .recovery import CRASH_GOAL, LATE_WORKER_GOAL, MISSING_EVIDENCE_GOAL, FaultController

BASE_SHA = "d497043810073d53a6ebaa94ba550a4c2c6b9b08"
SCENARIOS = ["S1", "S2", "S3", "S4", "S5", "S6"]
READ_ONLY_GOAL = {"title": "Check balance", "description": "Read the synthetic account balance"}


class Demo:
    def __init__(self, stack: DemoStack, browser: DemoBrowser, out: Path) -> None:
        self.stack, self.browser, self.out = stack, browser, out
        self.fault = FaultController(stack, TOKEN)
        self.results: list[dict] = []
        self.current: dict = {}
        self.api_events: list[dict] = []
        self.runs: dict[str, str] = {}

    def check(self, name: str, condition: bool) -> None:
        self.current["assertions"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(f"{self.current['id']}: {name}")

    def request(self, method: str, path: str, body=None, token: str | None = TOKEN,
                timeout: float = 30) -> tuple[int, dict]:
        response = httpx.request(method, self.stack.backend_url + path, json=body,
                                 headers={"X-Approval-Token": token} if token else {},
                                 trust_env=False, timeout=timeout)
        self.api_events.append({"method": method, "path": path, "status": response.status_code})
        return response.status_code, response.json()

    def get(self, run: dict | str) -> dict:
        run_id = run if isinstance(run, str) else run["run_id"]
        status, current = self.request("GET", f"/workflows/{run_id}")
        assert status == 200
        return current

    def snap(self, label: str) -> dict:
        snap = snapshot(self.stack.bind_dir / "trustflow.db")
        assert_integrity(snap)
        write_json(self.out / f"ledger-{label}.json", snap)
        return snap

    def consistent(self, run: dict, label: str) -> dict:
        current = self.get(run)
        status, audit = self.request("GET", f"/workflows/{run['run_id']}/audit")
        assert status == 200
        snap = self.snap(label)
        assert_api_matches(snap, current, audit["events"])
        self.check("api_audit_sqlite_consistent", True)
        return snap

    def wait_status(self, run: dict, expected: str, timeout: float = 20) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            current = self.get(run)
            if current["status"] == expected:
                return current
            if time.monotonic() >= deadline:
                raise AssertionError(f"expected {expected}, observed {current['status']}")
            time.sleep(0.05)

    @staticmethod
    def decision(run: dict, plan_digest: str | None = None) -> dict:
        return {"actor": "demo-manager", "plan_hash": plan_digest or run["plan"]["plan_hash"],
                "reason": "M5.2 deterministic synthetic demonstration"}

    def s1(self) -> None:
        before = self.snap("s1-before")
        run = self.browser.create(**READ_ONLY_GOAL)
        self.runs["S1"] = run["run_id"]
        self.check("low_risk_no_approval_completed", run["status"] == "COMPLETED"
                   and not run["approvals"] and all(s["risk"] == "LOW"
                                                    for s in run["plan"]["steps"]))
        self.check("deterministic_and_semantic_verified", bool(run["verification"]["passed"]))
        after = self.consistent(run, "s1-after")
        self.check("no_transfer_balances_unchanged", before["balances"] == after["balances"]
                   and before["transactions"] == after["transactions"])
        self.browser.capture("S1-low-risk-verified")

    def s2(self) -> None:
        before = self.snap("s2-before")
        run = self.browser.create("Transfer 5000", "transfer 5000 to bob")
        self.runs["S2"] = run["run_id"]
        self.browser.inspect_approval(run)
        self.browser.capture("S2-awaiting-exact-hash")
        self.check("high_risk_awaits_exact_hash", run["status"] == "AWAITING_APPROVAL"
                   and any(s["risk"] == "HIGH" for s in run["plan"]["steps"])
                   and plan_hash(run["plan"]) == run["plan"]["plan_hash"])
        path = f"/workflows/{run['run_id']}/approve"
        for name, tok, payload, expected in [
            ("missing_token_401", None, self.decision(run), 401),
            ("incorrect_token_403", "incorrect-demo-placeholder", self.decision(run), 403),
            ("wrong_or_stale_hash_409", TOKEN, self.decision(run, "0" * 64), 409),
        ]:
            status, _ = self.request("POST", path, payload, token=tok)
            self.check(name, status == expected)
        self.check("invalid_decisions_never_mutate", self.get(run)["status"] == "AWAITING_APPROVAL"
                   and self.snap("s2-invalid")["transactions"] == before["transactions"])
        run = self.browser.approve(run)
        self.check("ui_correct_approval_verified", run["status"] == "COMPLETED"
                   and run["verification"]["passed"])
        after = self.consistent(run, "s2-approved")
        txs = transactions_for(after, run)
        self.check("exactly_one_5000_transfer_approved_hash", len(txs) == 1
                   and txs[0]["amount"] == 5000 and run["approved_hash"] == plan_hash(run["plan"]))
        self.browser.capture("S2-approved-verified")
        status, _ = self.request("POST", path, self.decision(run))
        self.check("duplicate_approval_409_no_transfer", status == 409 and
                   self.snap("s2-replay")["transactions"] == after["transactions"])
        self.browser.refresh(run)
        self.browser.capture("S2-authoritative-refresh")

        # A separate fresh HIGH plan exercises two simultaneous valid decisions;
        # the primary workflow above retains its real UI approval interaction.
        race = self.browser.create("Transfer 1500 concurrently", "transfer 1500 to bob")
        self.runs["S2-concurrent"] = race["run_id"]
        barrier = threading.Barrier(2)

        def approve():
            barrier.wait(timeout=10)
            return self.request("POST", f"/workflows/{race['run_id']}/approve",
                                self.decision(race))[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve) for _ in range(2)]
            statuses = sorted(f.result(timeout=35) for f in futures)
        self.current["concurrent_statuses"] = statuses
        self.check("concurrent_valid_decisions_one_winner", statuses == [200, 409])
        race = self.get(race)
        raced = self.consistent(race, "s2-concurrent")
        self.check("concurrent_one_decision_one_transfer", race["status"] == "COMPLETED"
                   and len(race["approvals"]) == 1 and len(transactions_for(raced, race)) == 1)
        self.browser.refresh(race)
        self.browser.capture("S2-concurrent-one-winner")

    def s3(self) -> None:
        before = self.snap("s3-before")
        run = self.browser.create("Transfer 3000 rejected", "transfer 3000 to bob")
        self.runs["S3"] = run["run_id"]
        self.browser.inspect_approval(run)
        run = self.browser.reject(run)
        after = self.consistent(run, "s3-rejected")
        events = [e["action"] for e in after["audit"] if e["run_id"] == run["run_id"]]
        self.check("durable_rejected_no_successful_approval", run["status"] == "REJECTED"
                   and "approval_rejected" in events and "approval_granted" not in events)
        self.check("rejection_zero_transfer_unchanged_balances", not transactions_for(after, run)
                   and before["transactions"] == after["transactions"]
                   and before["balances"] == after["balances"])
        status, _ = self.request("POST", f"/workflows/{run['run_id']}/approve", self.decision(run))
        self.check("later_approval_409_no_transfer", status == 409
                   and self.snap("s3-late-approve")["transactions"] == after["transactions"])
        self.browser.refresh(run)
        self.browser.capture("S3-rejected-authoritative")

    def s4(self) -> None:
        self.current["fault_injected"] = True
        before = self.snap("s4-before")
        self.fault.arm("late_commit", semantic_failures=1)
        run = self.browser.create(**LATE_WORKER_GOAL)
        self.runs["S4"] = run["run_id"]
        self.fault.wait_checkpoint("worker_entered")
        self.check("real_timeout_execution_uncertain", run["status"] == "EXECUTION_UNCERTAIN")
        calls = self.fault.worker_calls(mutating_only=True)
        self.check("held_worker_no_commit_yet", self.snap("s4-held")["transactions"]
                   == before["transactions"])
        run = self.browser.recovery(run, "reconcile")
        self.check("live_worker_reconcile_fails_closed", run["status"] == "EXECUTION_UNCERTAIN"
                   and self.fault.worker_calls(mutating_only=True) == calls)
        self.browser.capture("S4-uncertain-fault-injected")
        self.fault.release()
        self.fault.wait_checkpoint("worker_returned")
        committed = self.snap("s4-late-commit")
        run = self.browser.recovery(run, "reconcile")
        self.check("ledger_reconcile_to_verification_pending", run["status"]
                   == "VERIFICATION_PENDING" and len(transactions_for(committed, run)) == 1)
        self.browser.capture("S4-verification-pending")
        run = self.browser.recovery(run, "verify-retry")
        self.check("verification_only_retry_completed", run["status"] == "COMPLETED"
                   and run["verification"]["passed"])
        # Concurrent repeat recoveries are harmless even after terminal completion.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.request, "POST", f"/workflows/{run['run_id']}/{action}", {})
                       for action in ("reconcile", "verify-retry")]
            outcomes = [f.result() for f in futures]
        self.check("concurrent_terminal_recovery_idempotent", all(
            status == 200 and r["status"] == "COMPLETED" for status, r in outcomes))
        after = self.consistent(run, "s4-after")
        self.check("recovery_no_mutating_invocation_or_duplicate", self.fault.worker_calls(
            mutating_only=True) == calls and after["transactions"] == committed["transactions"])
        self.browser.refresh(run)
        self.browser.capture("S4-recovered-no-duplicate")

    def s5(self) -> None:
        before = self.snap("s5-before")
        calls = self.fault.worker_calls()
        run = self.browser.create(**MISSING_EVIDENCE_GOAL)
        self.runs["S5"] = run["run_id"]
        self.check("real_mandatory_retrieval_missing_failed", run["goal"]["require_evidence"]
                   and run["status"] == "FAILED" and "retrieval_unavailable" in (run["error"] or "")
                   and not run["retrieval"] and run["plan"] is None)
        after = self.consistent(run, "s5-after")
        missing_events = [e for e in after["audit"] if e["run_id"] == run["run_id"]
                          and e["action"] == "mandatory_evidence_invalid"]
        self.check("missing_evidence_reason_in_audit", bool(missing_events) and any(
            "retrieval_unavailable" in e["detail"].get("codes", []) for e in missing_events))
        self.check("missing_evidence_blocks_all_tools_and_transfers",
                   self.fault.worker_calls() == calls
                   and after["transactions"] == before["transactions"]
                   and after["balances"] == before["balances"])
        self.browser.capture("S5-missing-mandatory-evidence")

    def s6(self) -> None:
        self.current["fault_injected"] = True
        self.current["interruption"] = "actual owned-container SIGKILL after SQLite COMMIT"
        before = self.snap("s6-before")
        self.fault.arm("after_commit")
        run = self.browser.create(**CRASH_GOAL)
        self.runs["S6"] = run["run_id"]
        self.browser.inspect_approval(run)
        self.browser.capture("S6-before-crash-approval")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, "POST", f"/workflows/{run['run_id']}/approve",
                                 self.decision(run), TOKEN, 150)
            checkpoint = self.fault.wait_checkpoint("after_commit")
            committed = self.snap("s6-at-crash-checkpoint")
            durable = committed["runs"][run["run_id"]]["run"]
            self.check("actual_commit_before_step_result_persisted",
                       durable["status"] == "EXECUTING"
                       and len(transactions_for(committed, durable)) == 1
                       and len(committed["transactions"]) == len(before["transactions"]) + 1
                       and transactions_for(committed, durable)[0]["tx_id"] == checkpoint["tx_id"]
                       and not any(r["tool_name"] == "simulate_transfer"
                                   and r["status"] == "SUCCEEDED"
                                   for r in durable["step_results"]))
            calls = self.fault.worker_calls(mutating_only=True)
            with self.browser.expect_backend_unavailable():
                self.stack.backend_lifecycle("kill")
                try:
                    response = future.result(timeout=20)
                except httpx.TransportError as exc:
                    self.current["interrupted_request"] = type(exc).__name__
                else:
                    self.check("interrupted_approval_did_not_report_success", response[0] != 200)
                self.stack.backend_lifecycle("start")
                run = self.wait_status(run, "VERIFICATION_PENDING")
                self.browser.refresh(run)
        self.check("restart_bound_hash_and_no_reexecution", run["approved_hash"]
                   == durable["approved_hash"] == plan_hash(run["approved_plan_snapshot"])
                   and self.fault.worker_calls(mutating_only=True) == calls)
        self.browser.capture("S6-after-sigkill-recovered-pending")
        run = self.browser.recovery(run, "verify-retry")
        after = self.consistent(run, "s6-after")
        self.check("crash_recovery_verified_same_transaction_ids", run["status"] == "COMPLETED"
                   and run["verification"]["passed"]
                   and after["transactions"] == committed["transactions"]
                   and self.fault.worker_calls(mutating_only=True) == calls)
        self.browser.refresh(run)
        self.browser.capture("S6-durable-ui-reload")
        # A separate graceful lifecycle check is labelled accurately.
        with self.browser.expect_backend_unavailable():
            self.stack.backend_lifecycle("restart")
            run = self.get(run)
            self.browser.refresh(run)
        restarted = self.consistent(run, "s6-graceful-restart")
        self.check("graceful_restart_preserves_all_tx_and_approvals", restarted["transactions"]
                   == after["transactions"] and run["approved_hash"] == durable["approved_hash"]
                   and self.fault.worker_calls(mutating_only=True) == calls)
        self.browser.capture("S6-after-graceful-restart")

    def run(self) -> list[dict]:
        for index, scenario in enumerate(SCENARIOS, 1):
            self.current = {"id": scenario, "verdict": "FAIL", "assertions": []}
            self.results.append(self.current)
            print(f"{scenario}: running real browser + SQLite assertions", flush=True)
            try:
                getattr(self, f"s{index}")()
                self.current["verdict"] = "PASS"
            except BaseException as exc:
                self.current["failure"] = sanitize(f"{type(exc).__name__}: {exc}")
                write_json(self.out / f"{scenario}-failure.json", {
                    "error": self.current["failure"], "traceback": traceback.format_exc()})
                # Preserve exact partial evidence; do not chain more mutations
                # after an unresolved fault or failed prerequisite assertion.
                break
            finally:
                write_json(self.out / "scenario-results.json", self.results)
                write_json(self.out / "api-log.json", self.api_events)
            print(f"{scenario}: {self.current['verdict']}", flush=True)
        for scenario in SCENARIOS[len(self.results):]:
            self.results.append({"id": scenario, "verdict": "NOT_RUN", "assertions": []})
        return self.results


def prerequisites(stack: DemoStack) -> dict:
    assert sys.version_info >= (3, 12), "Python 3.12+ required"
    if os.name == "nt" and len(str(stack._stack.marker_file)) >= 260:
        raise AssertionError(
            "Windows MAX_PATH: output directory is too deep for the owned workspace; "
            "rerun with a shorter --out path (for example C:/tf-demo). "
            "No Docker resources or SQLite storage have been created.")
    for executable in ("git", "docker"):
        assert shutil.which(executable), f"missing prerequisite: {executable}"
    from playwright.sync_api import sync_playwright

    with sync_playwright() as manager:
        assert Path(manager.chromium.executable_path).is_file(), (
            "Chromium missing: python -m playwright install chromium")
        with manager.chromium.launch(headless=True) as browser:
            chromium = browser.version
    tools = {}
    for name, argv in {"git": ["git", "--version"],
                       "docker_engine": ["docker", "version", "--format", "{{.Server.Version}}"],
                       "compose": ["docker", "compose", "version", "--short"]}.items():
        proc = stack.run(argv, timeout=60)
        assert proc.returncode == 0, f"{name} prerequisite failed: {sanitize(proc.stderr)}"
        tools[name] = proc.stdout.strip()
    config = stack.run([*stack._stack.base, "config", "--format", "json"], timeout=60)
    assert config.returncode == 0, "Compose must support !override (v2.24.4+)"
    return {"python": sys.version, "platform": platform.platform(), "tools": tools,
            "chromium": chromium, "packages": {name: importlib.metadata.version(name)
                for name in ("playwright", "fastapi", "pydantic", "httpx", "pytest")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("artifacts/m5.2/latest"))
    parser.add_argument("--compare", type=Path, help="Prior successful invocation directory")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    invocation = uuid.uuid4().hex
    out = new_attempt(args.out.resolve(), invocation)
    print(f"M5.2 invocation {invocation}; evidence: {out}", flush=True)
    # Production Dockerfile stays unchanged. Extra code exists only in the
    # explicitly labelled demo image with the same single owned /data mount.
    dockerfile = out / "Dockerfile.m52"
    dockerfile.write_text((root / "Dockerfile").read_text(encoding="utf-8")
                          + "\nCOPY scripts/m52 /app/demo\n", encoding="utf-8")
    stack = DemoStack(root, out, invocation, prepare=seed_owned, backend_dockerfile=dockerfile,
                      backend_command=["uvicorn", "demo.fault_backend:create_app", "--factory",
                                       "--host", "0.0.0.0", "--port", "8000"],
                      backend_env={"TRUSTFLOW_M52_RUN_ID": invocation})
    manifest = {"schema_version": 1, "invocation_id": invocation, "project": stack.project,
                "provider": "FakeLLMProvider", "llm_mode": "fake", "seed_id": SEED_ID,
                "base_sha": BASE_SHA, "scenario_ids": SCENARIOS,
                "started_at": datetime.now(UTC).isoformat()}
    failures: list[str] = []
    results: list[dict] = []
    before = None
    final: dict = {}
    demo = None
    browser = DemoBrowser(stack.frontend_url, stack.backend_url, out, TOKEN)
    try:
        identity = stack.run(["git", "rev-parse", "HEAD"], timeout=30)
        assert identity.returncode == 0
        manifest["git_sha"] = identity.stdout.strip()
        branch = stack.run(["git", "branch", "--show-current"], timeout=30)
        manifest["git_branch"] = branch.stdout.strip()
        status = stack.run(["git", "status", "--porcelain"], timeout=30)
        manifest["git_dirty"] = bool(status.stdout.strip())
        manifest["git_status"] = status.stdout.splitlines()
        ancestor = stack.run(["git", "merge-base", "--is-ancestor", BASE_SHA, "HEAD"], timeout=30)
        assert ancestor.returncode == 0, "accepted P2-05 baseline is not an ancestor"
        write_json(out / "environment.json", prerequisites(stack))
        before = stack.snapshot_preservation()
        write_json(out / "preservation-before.json", before)
        print("Prerequisites PASS; building isolated owned stack", flush=True)
        stack.up()
        initial = snapshot(stack.bind_dir / "trustflow.db")
        write_json(out / "ledger-before.json", initial)
        assert_integrity(initial)
        with browser:
            demo = Demo(stack, browser, out)
            results = demo.run()
            browser.assert_clean()
        failures.extend(f"{s['id']}: {s.get('failure', s['verdict'])}"
                        for s in results if s["verdict"] != "PASS")
    except BaseException as exc:
        failures.append(sanitize(f"{type(exc).__name__}: {exc}"))
        try:
            write_json(out / "failure.json", {"error": failures[-1],
                                              "traceback": traceback.format_exc()})
        except OSError as write_error:
            failures.append(sanitize(f"failure evidence unavailable: {write_error}"))
    finally:
        if not results and demo:
            results = demo.results
        for scenario in SCENARIOS[len(results):]:
            results.append({"id": scenario, "verdict": "NOT_RUN", "assertions": []})
        try:
            write_json(out / "scenario-results.json", results)
        except OSError as write_error:
            failures.append(sanitize(f"scenario evidence unavailable: {write_error}"))
        try:
            if (stack.bind_dir / "trustflow.db").is_file():
                final = snapshot(stack.bind_dir / "trustflow.db")
                assert_integrity(final)
                write_json(out / "ledger-after.json", final)
                write_json(out / "audit-summary.json", {"events": final["audit"],
                           "workflow_ids": demo.runs if demo else {}})
            if stack.container_ids:
                stack.runtime_logs()
                write_json(out / "fault-journal.json", FaultController(stack, TOKEN).status())
        except BaseException as exc:
            failures.append(sanitize(f"final evidence: {type(exc).__name__}: {exc}"))
        try:
            cleanup_errors = stack.teardown() if before is not None else []
            manifest["cleanup"] = {"passed": not cleanup_errors, "failures": cleanup_errors}
            failures.extend(cleanup_errors)
        except BaseException as exc:
            failures.append(sanitize(f"cleanup: {type(exc).__name__}: {exc}"))
            manifest["cleanup"] = {"passed": False, "failures": [failures[-1]]}
        try:
            if before is not None:
                manifest["preservation"] = stack.assert_preserved(before)
        except BaseException as exc:
            failures.append(sanitize(f"preservation: {type(exc).__name__}: {exc}"))
            manifest["preservation"] = {"unchanged": False}
    manifest["scenarios"] = results
    manifest["counts"] = {"passed": sum(s["verdict"] == "PASS" for s in results),
                          "failed": sum(s["verdict"] == "FAIL" for s in results),
                          "not_run": sum(s["verdict"] == "NOT_RUN" for s in results), "skipped": 0}
    readme = (
        "# M5.2 replay evidence\n\nSynthetic local demo; FakeLLMProvider only.\n\n"
        f"Invocation: `{invocation}`; exact SHA: `{manifest.get('git_sha', 'unknown')}`; "
        f"dirty: `{manifest.get('git_dirty', 'unknown')}`.\n\n"
        "Replay: `python scripts/run_m52_demo.py --out artifacts/m5.2/replay`\n\n"
        "Use `--compare <this-directory>` for normalized comparison. See docs/m52-demo.md "
        "for fault injection, seed, scenario narratives, screenshot map and limits.\n\n"
        "commands.jsonl records actual commands, cwd and exit codes. Browser logs exclude "
        "headers/bodies; no HAR/trace/video retains approval tokens. ledger-*.json are "
        "independent read-only SQLite transactions including WAL. S4 uses a real timed-out "
        "held worker; S6 uses actual SIGKILL after observed COMMIT, followed by a separately "
        "labelled graceful restart. Cleanup and original-resource preservation are mandatory.\n")
    passed = finalize(out, manifest, failures, final, readme, args.compare)
    print(f"M5.2 {'PASS' if passed else 'FAIL'}: {out}", flush=True)
    if failures:
        for failure in failures:
            print(f"FAIL: {sanitize(failure)}", flush=True)
    return 0 if passed else 1
