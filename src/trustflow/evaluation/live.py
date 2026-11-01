"""Bounded synthetic Muse evaluation. Default is a zero-request evaluation plan.

--offline uses MockTransport. Real inference requires BOTH --live and
--authorize-paid, after separate user authorization. No automatic batches.
Artifacts contain allowlisted observations, never raw requests or responses.
"""

import argparse
import asyncio
import json
import math
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx

from trustflow.agents.semantic import SemanticReviewer
from trustflow.api.app import Container, create_app
from trustflow.core.errors import TrustFlowError
from trustflow.core.logging import redact
from trustflow.core.settings import Settings
from trustflow.domain.models import WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider, MetaResponsesProvider
from trustflow.llm.usage import LiveCallBudget, UsageTracker

PROFILES = {
    "spark": ("muse-spark-1.3", "muse-spark-1.3"),
    "wonder": ("muse-wonder-1.0", "muse-wonder-1.0"),
    "mixed": ("muse-spark-1.3", "muse-wonder-1.0"),
}
TOKEN = "m53-synthetic-local-approval"
CASES = [("read", "Read Alice balance", "Inspect only the current USD balance of acc_alice."),
         ("approve", "Transfer 1500 USD", "Send exactly 1500 USD from acc_alice to acc_bob."),
         ("reject", "Transfer 3000 USD", "Send exactly 3000 USD from acc_alice to acc_bob."),
         ("recovery", "Transfer 1200 USD", "Send exactly 1200 USD from acc_alice to acc_bob.")]
CALL_FIELDS = ("purpose", "attempt", "transport_attempt", "model", "workflow_id", "request_id",
               "response_id", "http_status", "input_tokens", "output_tokens", "latency_ms",
               "estimated_cost_usd", "usage_unknown", "budget_charge_usd", "validation_outcome",
               "failure_reason", "over_budget")


def write_json(path: Path, data: Any) -> None:
    def clean(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value
    path.write_text(json.dumps(clean(data), indent=2), encoding="utf-8")


def source_identity() -> dict:
    root = Path(__file__).resolve().parents[3]

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
    return {"candidate_sha": git("rev-parse", "HEAD"),
            "candidate_tree": git("rev-parse", "HEAD^{tree}"),
            "working_tree_clean": not bool(git("status", "--porcelain=v1"))}


class InvalidFirstReview:
    """Explicit fault injection AFTER inference, with the incurred record kept.

    Used only by the recovery scenario, including in live runs. This proves
    application recovery, not a naturally observed model failure.
    """

    def __init__(self, provider):
        self.provider = provider
        self.injected = False

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def semantic_review(self, payload, **kwargs):
        result, record = self.provider.semantic_review(payload, **kwargs)
        if not self.injected:
            self.injected = True
            record.failure_reason = "injected_invalid_review"
            return {"verdict": "INVALID"}, record
        return result, record


def offline_transport() -> httpx.MockTransport:
    """Contract fixture, explicitly not evidence of real Muse behavior."""
    def handle(request):
        body = json.loads(request.content)
        prompt = body["input"]
        if body["text"]["format"]["name"] == "plan_steps":
            title = prompt.split("Goal: ", 1)[1].split(". Details:", 1)[0]
            steps, _ = FakeLLMProvider().propose_plan(title, "")
            defaults = {"account_id": "", "from_account": "", "to_account": "", "amount": 0,
                        "currency": "", "from_currency": "", "to_currency": "", "limit": 1}
            result = {"steps": [defaults | step for step in steps]}
        else:
            payload = json.loads(prompt.split("\n", 1)[1])
            expected = re.search(r"\d+", payload["goal"]["title"])
            transfers = [s["args"] for s in payload["plan_steps"]
                         if s["tool_name"] == "simulate_transfer"]
            ok = not expected or any(t["amount"] == float(expected[0]) for t in transfers)
            result = {"verdict": "PASS" if ok else "FAIL", "confidence": 1,
                      "issues": [] if ok else ["Goal amount differs from approved plan"],
                      "summary": "Offline contract fixture"}
        return httpx.Response(200, json={
            "id": "resp_mock_" + uuid.uuid4().hex, "status": "completed", "model": body["model"],
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }, headers={"x-request-id": "req_mock_" + uuid.uuid4().hex})
    return httpx.MockTransport(handle)


def sql_evidence(path: Path) -> dict:
    # Independent read-only connection. No executor-result claims are used.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute("SELECT tx_id,idempotency_key,from_account,to_account,amount,currency "
                            "FROM ledger_transactions ORDER BY seq").fetchall()
        return {"integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
                "transactions": [dict(zip(
                    ("tx_id", "operation_id", "from_account", "to_account", "amount", "currency"),
                    row, strict=True)) for row in rows],
                "total_balance": conn.execute(
                    "SELECT SUM(balance) FROM ledger_accounts").fetchone()[0]}
    finally:
        conn.close()


def summarize(run: dict, case: str, before: dict, after: dict) -> dict:
    verification = run.get("verification") or {}
    plan = run.get("plan") or {}
    return {"case": case, "workflow_id": run.get("run_id"), "status": run.get("status"),
            "plan_hash": plan.get("plan_hash"), "approved_hash": run.get("approved_hash"),
            "plan_steps": [{"tool_name": s["tool_name"], "args": s["args"], "risk": s["risk"]}
                           for s in plan.get("steps", [])],
            "deterministic_checks": verification.get("checks", []),
            "semantic_verdict": (verification.get("semantic") or {}).get("verdict"),
            "calls": [{k: r.get(k) for k in CALL_FIELDS} for r in run.get("llm_calls", [])],
            "ledger_before": before, "ledger_after": after,
            "failure_reason": run.get("error")}


def expected_plan(run: dict, amount: int | None) -> bool:
    steps = (run.get("plan") or {}).get("steps", [])
    transfers = [s["args"] for s in steps if s["tool_name"] == "simulate_transfer"]
    if amount is None:
        return not transfers and any(s["tool_name"] == "get_balance"
                                    and s["args"].get("account_id") == "acc_alice" for s in steps)
    return transfers == [{"from_account": "acc_alice", "to_account": "acc_bob",
                          "amount": float(amount), "currency": "USD"}]


async def evaluate_profile(directory: Path, profile: str, provider, mode: str) -> dict:
    directory.mkdir()
    db = directory / "synthetic.db"
    planning, review = PROFILES.get(profile, ("muse-spark-1.3", "muse-spark-1.3"))
    cap = (provider._settings.max_output_tokens
           if isinstance(provider, MetaResponsesProvider) else 1200)
    isolated: dict[str, Any] = {"_env_file": None}
    settings = Settings(**isolated, llm_mode="fake" if mode == "fake" else "live",
                        db_path=str(db), approval_token=TOKEN, planning_model=planning,
                        review_model=review, llm_max_retries=0, max_retries=0,
                        semantic_max_attempts=2, token_budget_per_workflow=30000,
                        max_output_tokens=cap)
    c = Container(settings, provider=provider)
    app = create_app(c)
    rows = []
    approved_run = None
    assert c.engine.semantic is not None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://trustflow.local") as api:
        for case, title, description in CASES:
            before = sql_evidence(db)
            c.engine.semantic._llm = (
                InvalidFirstReview(provider) if case == "recovery" else provider)
            response = await api.post("/workflows", json={"title": title,
                                                        "description": description})
            run = response.json()
            amount = None if case == "read" else {"approve": 1500, "reject": 3000,
                                                    "recovery": 1200}[case]
            matches = expected_plan(run, amount)
            pending_observed = None
            unchanged = False
            replay_code = None
            wrong_hash_code = None
            # Approve only the fixture's exact authorized semantics. Every
            # other model-proposed plan is measured as a failure, never accepted.
            if matches and case != "read" and run["status"] == "AWAITING_APPROVAL":
                path = f"/workflows/{run['run_id']}"
                decision = {"actor": "evaluation-operator", "plan_hash": run["plan"]["plan_hash"]}
                headers = {"X-Approval-Token": TOKEN}
                wrong = await api.post(path + "/approve", json=decision | {"plan_hash": "0" * 64},
                                       headers=headers)
                wrong_hash_code = wrong.status_code
                action = "/reject" if case == "reject" else "/approve"
                response = await api.post(path + action, json=decision, headers=headers)
                run = response.json()
                replay_code = (await api.post(path + action, json=decision,
                                              headers=headers)).status_code
                if case == "recovery":
                    pending_observed = run["status"]
                    committed = sql_evidence(db)
                    response = await api.post(path + "/verify-retry", headers=headers)
                    run = response.json()
                    unchanged = sql_evidence(db) == committed
                if case == "approve":
                    approved_run = c.repo.get(run["run_id"])
            after = sql_evidence(db)
            row = summarize(run, case, before, after)
            expected_status = "REJECTED" if case == "reject" else "COMPLETED"
            new_txs = after["transactions"][len(before["transactions"]):]
            ledger_ok = (not new_txs) if amount is None or case == "reject" else (
                len(new_txs) == 1 and new_txs[0]["amount"] == amount
                and new_txs[0]["from_account"] == "acc_alice"
                and new_txs[0]["to_account"] == "acc_bob"
                and new_txs[0]["currency"] == "USD")
            row["checks"] = {"task_semantics": matches,
                             "status": run.get("status") == expected_status,
                             "ledger_effect": ledger_ok,
                             "ledger_integrity": after["integrity"] == "ok",
                             "conservation": after["total_balance"] == 16200}
            if case != "read":
                row["checks"] |= {"wrong_hash_rejected": wrong_hash_code == 409,
                                  "decision_replay_rejected": replay_code == 409}
            if case in {"approve", "recovery"}:
                row["checks"]["exact_approved_hash"] = bool(run.get("approved_hash")) and (
                    run["approved_hash"] == (
                        run.get("approved_plan_snapshot") or {}).get("plan_hash")
                    == (run.get("plan") or {}).get("plan_hash"))
            if case == "recovery":
                row["injected_fault"] = "first semantic result replaced after inference"
                row["checks"] |= {"pending_before_retry": (
                                      pending_observed == "VERIFICATION_PENDING"),
                                  "verification_only": pending_observed is not None and unchanged}
            row["passed"] = all(row["checks"].values())
            rows.append(row)
            write_json(directory / "cases.json", rows)
            # A safety failure terminates this finite profile immediately.
            if not ledger_ok or after["total_balance"] != 16200:
                break
        if approved_run is not None:
            assert approved_run.approved_plan_snapshot is not None
            tracker = UsageTracker(30000, "semantic-negative-" + profile)
            before = sql_evidence(db)
            try:
                reviewer = SemanticReviewer(provider, c.registry.ledger, max_output_tokens=cap)
                result = reviewer.review(
                    WorkflowGoal(title="Transfer 1700 USD",
                                 description="Send exactly 1700 USD from acc_alice to acc_bob."),
                    approved_run.approved_plan_snapshot, tracker=tracker)
                verdict, failure = result.verdict, None
            except TrustFlowError:
                verdict, failure = None, "semantic_review_unavailable"
            rows.append({"case": "semantic_negative", "semantic_verdict": verdict,
                         "passed": verdict == "FAIL" and sql_evidence(db) == before,
                         "failure_reason": failure, "expected": "FAIL: goal 1700 vs approved 1500",
                         "calls": [{k: r.model_dump().get(k) for k in CALL_FIELDS}
                                   for r in tracker.records]})
    return {"profile": profile, "mode": mode, "cases": rows,
            "passed": len(rows) == 5 and all(r["passed"] for r in rows),
            "task_completion": sum(r["passed"] for r in rows), "task_count": 5}


def evaluation_plan(profiles: list[str], requests: int, cost: float) -> dict:
    return {"mode": "plan_only", "paid_requests_executed": 0, "profiles": profiles,
            "models": {p: dict(zip(("planning", "review"), PROFILES[p], strict=True))
                       for p in profiles},
            "cases": [c[0] for c in CASES] + ["semantic_negative"],
            "expected_requests_per_profile": 9, "maximum_http_requests": requests,
            "maximum_estimated_cost_usd": cost,
            "pricing_usd_per_million": {"muse-spark-1.3": {"input": 1.25, "output": 4.25},
                                        "muse-wonder-1.0": {"input": 4, "output": 20}},
            "caveat": "Cost estimates are advisory; unknown usage retains reservations. "
                      "No exact provider billing ceiling is guaranteed. "
                      "A request cap can intentionally stop before all profiles complete."}


async def execute(args, directory: Path) -> dict:
    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    budget = LiveCallBudget(args.max_requests, args.max_cost_usd)
    report = evaluation_plan(profiles, args.max_requests, args.max_cost_usd)
    report["mode"] = "live" if args.live else "offline_mock"
    report["profiles"] = []
    report["completed"] = False
    report.update(source_identity())
    try:
        result = await execute_profiles(args, directory, profiles, budget, report)
        report["completed"] = True
        return result
    finally:
        # Also retain session counters if interrupted after a paid attempt.
        report["paid_requests_executed"] = budget.requests if args.live else 0
        report["http_attempts"] = budget.requests
        report["session_estimated_charge_usd"] = round(budget.charged_usd, 8)
        write_json(directory / "report.json", report)


async def execute_profiles(args, directory: Path, profiles: list[str],
                           budget: LiveCallBudget, report: dict) -> dict:
    for profile in profiles:
        planning, review = PROFILES[profile]
        isolated: dict[str, Any] = {"_env_file": None}
        provider = MetaResponsesProvider(
            Settings(**isolated, llm_mode="live",
                     planning_model=planning, review_model=review,
                     max_output_tokens=args.max_output_tokens, llm_max_retries=args.retries,
                     llm_timeout_seconds=30),
            transport=None if args.live else offline_transport(), budget=budget)
        result = await evaluate_profile(directory / profile, profile, provider, report["mode"])
        report["profiles"].append(result)
        report["paid_requests_executed"] = budget.requests if args.live else 0
        report["http_attempts"] = budget.requests
        report["session_estimated_charge_usd"] = round(budget.charged_usd, 8)
        # Persist completed evidence before continuing a bounded matrix.
        write_json(directory / "report.json", report)
        if budget.requests >= args.max_requests:
            break
    report["fake_comparator"] = await evaluate_profile(
        directory / "fake", "fake", FakeLLMProvider(), "fake")
    report["paid_requests_executed"] = budget.requests if args.live else 0
    report["http_attempts"] = budget.requests
    report["session_estimated_charge_usd"] = round(budget.charged_usd, 8)
    report["passed"] = len(report["profiles"]) == len(profiles) and all(
        r["passed"] for r in report["profiles"])
    report["comparison"] = [
        {"profile": p["profile"], "case": r["case"], "model_passed": r["passed"],
         "fake_passed": next((f["passed"] for f in report["fake_comparator"]["cases"]
                               if f["case"] == r["case"]), None)}
        for p in report["profiles"] for r in p["cases"]]
    calls = [call for p in report["profiles"] for case in p["cases"] for call in case["calls"]]
    report["model_metrics"] = model_metrics(calls)
    return report


def model_metrics(calls: list[dict]) -> dict:
    metrics = {}
    for model in sorted({c["model"] for c in calls}):
        rows = [c for c in calls if c["model"] == model]
        unknown = any(c["usage_unknown"] for c in rows)
        metrics[model] = {
            "http_attempts": len(rows), "usage_unknown": unknown,
            "input_tokens": None if unknown else sum(c["input_tokens"] for c in rows),
            "output_tokens": None if unknown else sum(c["output_tokens"] for c in rows),
            "partial_known_cost_usd": round(sum(c["estimated_cost_usd"] for c in rows), 8),
            "estimated_total_cost_usd": None if unknown else round(
                sum(c["estimated_cost_usd"] for c in rows), 8),
            "mean_latency_ms": round(sum(c["latency_ms"] for c in rows) / len(rows), 2),
            "typed_valid": sum(c["validation_outcome"] in {
                "typed_plan_valid", "typed_review_valid"} for c in rows),
            "invalid_output": sum(c["validation_outcome"] in {
                "failed", "typed_plan_invalid", "typed_review_invalid"} for c in rows),
            "injected_invalid": sum(c["failure_reason"] == "injected_invalid_review" for c in rows),
        }
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true")
    group.add_argument("--offline", action="store_true")
    parser.add_argument("--authorize-paid", action="store_true",
                        help="Operator confirms separate authorization for this bounded invocation")
    parser.add_argument("--profile", choices=[*PROFILES, "all"], default="spark")
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--max-cost-usd", type=float, default=0.25)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--retries", type=int, choices=range(4), default=0)
    parser.add_argument("--out", type=Path, default=Path("artifacts/m53"))
    args = parser.parse_args(argv)
    if args.live != args.authorize_paid:
        parser.error("Real inference requires --live AND --authorize-paid after user authorization")
    if not 1 <= args.max_requests <= 1000 or not math.isfinite(args.max_cost_usd) \
            or not 0 < args.max_cost_usd <= 100 or not 16 <= args.max_output_tokens <= 16384:
        parser.error("Invalid request, cost or output-token limit")
    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    if not args.live and not args.offline:
        print(json.dumps(evaluation_plan(profiles, args.max_requests, args.max_cost_usd), indent=2))
        return 0
    if args.live and not os.environ.get("MODEL_API_KEY"):
        parser.error("MODEL_API_KEY must be provided by the environment")
    directory = args.out.resolve() / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    try:
        report = asyncio.run(execute(args, directory))
        write_json(directory / "report.json", report)
        print(json.dumps({"report": str(directory / "report.json"), "mode": report["mode"],
                          "passed": report["passed"], "http_attempts": report["http_attempts"],
                          "estimated_charge_usd": report["session_estimated_charge_usd"]}))
        return 0 if report["passed"] else 1
    except Exception as exc:
        # No traceback, arbitrary exception text, headers or body artifacts.
        write_json(directory / "FAILED.json", {"failure_reason": "evaluation_interrupted",
                                               "error_type": type(exc).__name__})
        print(json.dumps({"failure_reason": "evaluation_interrupted", "directory": str(directory)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
