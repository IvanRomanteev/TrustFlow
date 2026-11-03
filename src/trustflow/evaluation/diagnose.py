"""One Spark planning request, typed validation only; never executes tools.

Default: zero-request plan. --offline: MockTransport. Paid inference requires
--live --authorize-paid AND separate operator authorization. No repair or retry.
"""

import argparse
import json
import math
import os
import uuid
from contextlib import closing
from pathlib import Path

from trustflow.agents.planner import validate_steps
from trustflow.core.errors import LLMResponseError, PlanValidationError, TrustFlowError
from trustflow.domain.models import Plan
from trustflow.evaluation.live import (
    CALL_FIELDS,
    EvaluationSettings,
    offline_transport,
    source_identity,
    sql_evidence,
    write_json,
)
from trustflow.llm.provider import MetaResponsesProvider
from trustflow.llm.usage import LiveCallBudget, LLMCallRecord, UsageTracker
from trustflow.persistence import PersistentLedger
from trustflow.policies.policy_engine import PolicyEngine

TITLE = "Read Alice balance"
DESCRIPTION = "Inspect only the current USD balance of acc_alice."


def execute(args, directory: Path) -> dict:
    """The single propose_plan call is the only possible inference site."""
    budget = LiveCallBudget(1, args.max_cost_usd)
    settings = EvaluationSettings(
        llm_mode="live", planning_model="muse-spark-1.3", review_model="muse-spark-1.3",
        llm_max_retries=0, llm_max_requests=1, llm_max_cost_usd=args.max_cost_usd,
        max_output_tokens=args.max_output_tokens, llm_reasoning_effort=args.reasoning_effort)
    provider = MetaResponsesProvider(settings, budget=budget,
                                     transport=None if args.live else offline_transport())
    tracker = UsageTracker(30000, "spark-diagnostic-" + uuid.uuid4().hex)
    report = {"mode": "live_diagnostic" if args.live else "offline_diagnostic",
              "profile": "spark", "model": "muse-spark-1.3", "maximum_http_attempts": 1,
              "retries": 0, "repair_attempts": 0, "reasoning_effort": args.reasoning_effort,
              "max_output_tokens": args.max_output_tokens,
              "maximum_estimated_cost_usd": args.max_cost_usd,
              "typed_plan_valid": False, "matches_read_goal": False, "passed": False,
              "live_integration_pass": False, "tool_executions": 0,
              "limitation": "One planning diagnostic; no workflow execution or semantic review.",
              **source_identity()}
    # Caller creates an exclusive UUID directory: this DB cannot be a reused ledger.
    db = directory / "synthetic.db"
    if db.exists():
        raise ValueError("Diagnostic storage must be fresh")
    ledger = PersistentLedger(str(db))
    with closing(ledger._conn):
        report["ledger_before"] = sql_evidence(db)
        try:
            tracker.reserve((len(TITLE) + len(DESCRIPTION)) // 4 + 256 + args.max_output_tokens)
            raw, record = provider.propose_plan(TITLE, DESCRIPTION, tracker=tracker)
            tracker.record(record)
            try:
                plan = PolicyEngine(settings).apply(
                    Plan(goal_title=TITLE, steps=validate_steps(raw)))
            except PlanValidationError:
                record.validation_outcome = "typed_plan_invalid"
                record.failure_reason = "plan_validation_failed"
                raise
            record.validation_outcome = "typed_plan_valid"
            report["typed_plan_valid"] = True
            report["matches_read_goal"] = len(plan.steps) == 1 and (
                plan.steps[0].tool_name == "get_balance"
                and plan.steps[0].args == {"account_id": "acc_alice"})
        except LLMResponseError as exc:
            # Tracker-owned failures carry no record; pre-return failures may do so.
            if isinstance(exc.record, LLMCallRecord):
                tracker.record(exc.record)
            report["failure_reason"] = "provider_response_failed"
        except TrustFlowError:
            report["failure_reason"] = "typed_validation_or_budget_failed"
        finally:
            report["calls"] = [{k: r.model_dump().get(k) for k in CALL_FIELDS}
                               for r in tracker.records]
            report["http_attempts"] = budget.requests
            report["paid_requests_executed"] = budget.requests if args.live else 0
            report["session_estimated_charge_usd"] = round(budget.charged_usd, 8)
            report["ledger_after"] = sql_evidence(db)
            report["ledger_unchanged"] = report["ledger_before"] == report["ledger_after"]
            report["passed"] = (report["typed_plan_valid"] and report["matches_read_goal"]
                                and report["ledger_unchanged"] and budget.requests == 1)
            write_json(directory / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--offline", action="store_true")
    parser.add_argument("--authorize-paid", action="store_true")
    parser.add_argument("--profile", choices=["spark"], default="spark")
    parser.add_argument("--max-requests", type=int, choices=[1], default=1)
    parser.add_argument("--retries", type=int, choices=[0], default=0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--reasoning-effort", choices=["minimal", "low"], default="low")
    parser.add_argument("--max-cost-usd", type=float, default=0.03)
    parser.add_argument("--out", type=Path, default=Path("artifacts/m53-diagnostic"))
    args = parser.parse_args(argv)
    if args.live != args.authorize_paid:
        parser.error("Paid diagnostic requires --live AND --authorize-paid "
                     "after user authorization")
    if not math.isfinite(args.max_cost_usd) or not 0 < args.max_cost_usd <= 0.05 \
            or not 16 <= args.max_output_tokens <= 4096:
        parser.error("Diagnostic requires output cap 16..4096 and estimated cost (0, 0.05] USD")
    if not args.live and not args.offline:
        print(json.dumps({"mode": "diagnostic_plan_only", "paid_requests_executed": 0,
                          "profile": "spark", "maximum_http_attempts": 1, "retries": 0,
                          "max_output_tokens": args.max_output_tokens,
                          "reasoning_effort": args.reasoning_effort,
                          "maximum_estimated_cost_usd": args.max_cost_usd}))
        return 0
    if args.live and not os.environ.get("MODEL_API_KEY"):
        parser.error("MODEL_API_KEY must be provided by the environment")
    directory = args.out.resolve() / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    try:
        report = execute(args, directory)
        print(json.dumps({"report": str(directory / "report.json"), "passed": report["passed"],
                          "http_attempts": report["http_attempts"],
                          "live_integration_pass": False}))
        return 0 if report["passed"] else 1
    except Exception as exc:
        # Never print exception text, raw responses or a traceback.
        write_json(directory / "FAILED.json", {"error_type": type(exc).__name__})
        print(json.dumps({"failure_reason": "diagnostic_interrupted", "directory": str(directory)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
