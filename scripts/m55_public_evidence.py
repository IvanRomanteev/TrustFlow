"""Offline, allowlisted M5.5 projection and verification. Never calls a provider.

Default: verify the public package. --source: also reproduce it from retained
records (immutable read-only SQLite). --write: create the projection initially.
Raw requests, responses, reasoning, credentials and account owners are excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path

SOURCE_SHA = "aacfed074a4116dbded98ee88f783226a910e55c"
SESSION = "0bb6cdb63c03418a85f873755ed86200"
DEFAULT = Path(__file__).resolve().parents[1] / "docs/submission/evidence/m55"
RATES = {"muse-spark-1.3": ("1.25", "4.25"), "muse-wonder-1.0": ("4", "20")}
CALL_FIELDS = (
    "model",
    "purpose",
    "workflow_id",
    "attempt",
    "transport_attempt",
    "http_status",
    "input_tokens",
    "output_tokens",
    "estimated_cost_usd",
    "budget_charge_usd",
    "usage_unknown",
    "over_budget",
    "validation_outcome",
    "failure_reason",
)
WIRE_FIELDS = (
    "requested_model",
    "response_model",
    "response_model_matches",
    "http_status",
    "transport",
    "started_utc",
    "finished_utc",
)
TX_FIELDS = ("tx_id", "operation_id", "from_account", "to_account", "amount", "currency")
CASE_FIELDS = (
    "case",
    "workflow_id",
    "status",
    "plan_hash",
    "approved_hash",
    "semantic_verdict",
    "passed",
    "injected_fault",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def select(data: dict, fields) -> dict:
    return {key: data[key] for key in fields if key in data}


def canonical(plan: dict) -> dict:
    # Same hash payload as Plan.canonical at SOURCE_SHA; no rationale or reasoning.
    return {
        "goal_title": plan["goal_title"],
        "version": plan["version"],
        "steps": [
            select(step, ("step_id", "tool_name", "intent_id", "operation_id", "args"))
            for step in plan["steps"]
        ],
    }


def plan_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def ledger_observation(value: dict) -> dict:
    return {
        "integrity": value["integrity"],
        "total_balance": value["total_balance"],
        "transactions": [select(tx, TX_FIELDS) for tx in value["transactions"]],
    }


def project(source: Path) -> dict:
    session = source / "live-sessions" / SESSION
    sources = {}

    def record(path):
        sources[path.relative_to(source).as_posix()] = digest(path)
        return read(path)

    session_data = record(session / "session.json")
    assert session_data["source_sha"] == SOURCE_SHA and session_data["passed"] is True
    seal = record(source / "runners-seal.json")
    provenance = record(source / "runner-provenance.json")
    for name, expected in seal.items():
        assert Path(name).name == name and name.endswith(".py")
        assert digest(source / name) == expected
        sources[name] = expected
    assert provenance["candidate_sha"] == SOURCE_SHA
    stages = []
    for report_path in sorted(session.glob("*/*/report.json")):
        report = record(report_path)
        stage = report_path.parent.parent.name
        matrix = stage == "spark-matrix"
        assert report["candidate_sha" if matrix else "candidate"] == SOURCE_SHA
        assert report["mode"] == "live" and report["passed"] is True
        assert report["candidate_tree" if matrix else "tree"] == provenance["tree"]
        assert report["working_tree_clean" if matrix else "clean"] is True
        runner = "bounded_matrix.py" if matrix else "recovery_probe.py"
        assert report["runner_sha256" if matrix else "script_sha256"] == seal[runner]
        calls = (
            [a["call"] for a in record(report_path.parent / "attempts.json")]
            if matrix
            else report["calls"]
        )
        wires = (
            record(report_path.parent / "wire-models.json") if matrix else [report["wire_witness"]]
        )
        assert len(calls) == len(wires) == report["http_attempts"]
        public_calls = []
        for call, wire in zip(calls, wires, strict=True):
            entry = select(call, CALL_FIELDS)
            entry["response_status"] = call["response_metadata"]["response_status"]
            # Identifiers are useful for uniqueness, but original provider IDs stay private.
            for key in ("request_id", "response_id"):
                assert call[key]
                entry[key + "_sha256"] = hashlib.sha256(call[key].encode()).hexdigest()
            entry["wire"] = select(wire, WIRE_FIELDS)
            public_calls.append(entry)

        db = report_path.parent / ("profile/synthetic.db" if matrix else "synthetic.db")
        assert not any(p.stat().st_size for p in db.parent.glob(db.name + "-wal"))
        before = digest(db)
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            accounts = [
                dict(row)
                for row in connection.execute(
                    "SELECT account_id,balance,currency FROM ledger_accounts ORDER BY account_id"
                )
            ]
            txs = [
                dict(row)
                for row in connection.execute(
                    "SELECT tx_id,idempotency_key AS operation_id,"
                    "from_account,to_account,amount,currency "
                    "FROM ledger_transactions ORDER BY seq"
                )
            ]
            runs = []
            for row in connection.execute("SELECT run_json FROM runs ORDER BY run_id"):
                run = json.loads(row[0])
                snap = run.get("approved_plan_snapshot")
                runs.append(
                    {
                        "run_id": run["run_id"],
                        "status": run["status"],
                        "approved_hash": run["approved_hash"],
                        "approved_canonical": canonical(snap) if snap else None,
                        "plan_hash": run["plan"]["plan_hash"],
                        "approvals": [
                            select(a, ("plan_version", "plan_hash", "decision"))
                            for a in run["approvals"]
                        ],
                    }
                )
        finally:
            connection.close()
        assert digest(db) == before
        sources[db.relative_to(source).as_posix()] = before
        entry = {
            "stage": stage,
            "calls": public_calls,
            "sqlite": {"integrity": "ok", "accounts": accounts, "transactions": txs, "runs": runs},
        }
        if matrix:
            entry["cases"] = []
            for case in report["profile_result"]["cases"]:
                item = select(case, CASE_FIELDS)
                for key in ("ledger_before", "ledger_after"):
                    if key in case:
                        item[key] = ledger_observation(case[key])
                entry["cases"].append(item)
        else:
            entry.update(
                select(report, ("planning", "initial_review", "status", "semantic_verdict"))
            )
            for key in ("ledger_before", "ledger_after"):
                entry[key] = ledger_observation(report[key])
        stages.append(entry)
    return {
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "source_tree": provenance["tree"],
        "session": SESSION,
        "mode": "retained-live-records",
        "historical_rates_usd_per_million": RATES,
        "configuration": select(
            session_data["planned_scope"],
            (
                "max_http_attempts",
                "advisory_limit_usd",
                "transport_retries",
                "planning_repairs",
                "max_output_tokens",
                "reasoning_effort",
            ),
        ),
        "sources_sha256": sources,
        "stages": stages,
    }


def verify(data: dict) -> dict:
    assert data["source_sha"] == SOURCE_SHA and data["session"] == SESSION
    assert data["historical_rates_usd_per_million"] == {k: list(v) for k, v in RATES.items()}
    assert data["configuration"] == {
        "max_http_attempts": 11,
        "advisory_limit_usd": 0.33,
        "transport_retries": 0,
        "planning_repairs": 0,
        "max_output_tokens": 4096,
        "reasoning_effort": "low",
    }
    stages = {s["stage"]: s for s in data["stages"]}
    assert set(stages) == {"spark-matrix", "spark-recovery", "wonder-recovery"}
    calls = [c for stage in stages.values() for c in stage["calls"]]
    assert Counter(c["model"] for c in calls) == {"muse-spark-1.3": 10, "muse-wonder-1.0": 1}
    assert len({c["response_id_sha256"] for c in calls}) == len(calls) == 11
    cost = Decimal(0)
    for call in calls:
        assert call["http_status"] == 200 and call["response_status"] == "completed"
        assert (
            call["transport_attempt"] == 1 and not call["usage_unknown"] and not call["over_budget"]
        )
        wire = call["wire"]
        assert wire["requested_model"] == wire["response_model"] == call["model"]
        assert wire["response_model_matches"] is True and wire["http_status"] == 200
        assert wire["transport"] == "HTTPTransport"
        assert wire["started_utc"] < wire["finished_utc"]
        if call["failure_reason"] == "injected_invalid_review":
            assert call["purpose"] == "semantic_review"
        else:
            assert call["failure_reason"] is None
            assert call["validation_outcome"] in ("typed_plan_valid", "typed_review_valid")
        rates = RATES[call["model"]]
        amount = (
            sum(
                Decimal(call[k]) * Decimal(rate)
                for k, rate in zip(("input_tokens", "output_tokens"), rates, strict=True)
            )
            / 1_000_000
        )
        assert abs(amount - Decimal(str(call["estimated_cost_usd"]))) < Decimal("0.000000001")
        assert abs(amount - Decimal(str(call["budget_charge_usd"]))) < Decimal("0.000000001")
        cost += amount
    tokens = {k: sum(c[k] for c in calls) for k in ("input_tokens", "output_tokens")}
    assert tokens == {"input_tokens": 11042, "output_tokens": 11299}
    assert cost == Decimal("0.073201")
    for name, stage in stages.items():
        db = stage["sqlite"]
        txs = db["transactions"]
        assert db["integrity"] == "ok"
        assert sum(Decimal(str(a["balance"])) for a in db["accounts"]) == 16200
        assert {a["account_id"] for a in db["accounts"]} == {"acc_alice", "acc_bob", "acc_ops"}
        assert len({t["operation_id"] for t in txs}) == len(txs)
        assert sorted(t["amount"] for t in txs) == (
            [1200, 1500] if name == "spark-matrix" else [1200]
        )
        bound = set()
        for run in db["runs"]:
            payload = run["approved_canonical"]
            if payload is None:
                assert not run["approved_hash"]
                continue
            assert plan_hash(payload) == run["approved_hash"] == run["plan_hash"]
            assert payload["goal_title"] in ("Transfer 1200 USD", "Transfer 1500 USD")
            assert run["status"] == "COMPLETED"
            assert any(
                a["decision"] == "APPROVED"
                and a["plan_hash"] == run["approved_hash"]
                and a["plan_version"] == payload["version"]
                for a in run["approvals"]
            )
            for step in payload["steps"]:
                if step["tool_name"] != "simulate_transfer":
                    continue
                matches = [t for t in txs if t["operation_id"] == step["operation_id"]]
                assert len(matches) == 1 and step["intent_id"] == step["operation_id"]
                assert all(matches[0][k] == step["args"][k] for k in TX_FIELDS[2:])
                bound.add(step["operation_id"])
        assert bound == {t["operation_id"] for t in txs}
        if name != "spark-matrix":
            assert len(stage["calls"]) == 1 and stage["calls"][0]["purpose"] == "semantic_review"
            assert stage["planning"] == "fixed offline fixture; no real planning inference"
            assert stage["initial_review"] == "local pre-inference outage; no real first review"
            assert stage["ledger_before"] == stage["ledger_after"]
            assert stage["ledger_after"]["transactions"] == txs
    cases = stages["spark-matrix"]["cases"]
    assert [c["case"] for c in cases] == [
        "read",
        "approve",
        "reject",
        "recovery",
        "semantic_negative",
    ]
    assert all(c["passed"] is True for c in cases)
    runs = {r["run_id"]: r for r in stages["spark-matrix"]["sqlite"]["runs"]}
    for case in cases[:4]:
        run = runs[case["workflow_id"]]
        for key in ("status", "plan_hash", "approved_hash"):
            assert case[key] == run[key]
        for key in ("ledger_before", "ledger_after"):
            assert case[key]["integrity"] == "ok" and case[key]["total_balance"] == 16200
    assert cases[0]["ledger_before"] == cases[0]["ledger_after"]
    assert cases[2]["ledger_before"] == cases[2]["ledger_after"]
    assert cases[3]["injected_fault"] == "first semantic result replaced after inference"
    for case, expected_amount in ((cases[1], 1500), (cases[3], 1200)):
        before = case["ledger_before"]["transactions"]
        after = case["ledger_after"]["transactions"]
        assert after[: len(before)] == before and len(after) == len(before) + 1
        assert after[-1]["amount"] == expected_amount
    assert (
        cases[3]["ledger_after"]["transactions"] == stages["spark-matrix"]["sqlite"]["transactions"]
    )
    assert cases[4]["semantic_verdict"] == "FAIL"
    return {
        "calls": len(calls),
        **tokens,
        "estimated_cost_usd": str(cost),
        "sqlite_projections": len(stages),
        "spark_cases": len(cases),
    }


def main() -> None:
    if not __debug__:
        raise SystemExit("Run without -O: evidence assertions must remain enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT)
    parser.add_argument("--source", type=Path, help="retained m55-live-video-20261104 directory")
    parser.add_argument(
        "--write", action="store_true", help="create evidence.json; refuses overwrite"
    )
    args = parser.parse_args()
    target = args.package / "evidence.json"
    if args.write:
        assert args.source and not target.exists()
        projected = project(args.source.resolve())
        data = json.loads(json.dumps(projected))
        verify(data)
        args.package.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    data = read(target)
    summary = verify(data)
    if args.source:
        assert json.loads(json.dumps(project(args.source.resolve()))) == data
        summary["retained_source_projection_identical"] = True
    if not args.write:
        manifest = read(args.package / "manifest.json")
        assert set(manifest["public_files_sha256"]) == {
            "evidence.json",
            "README.md",
            "../../../../scripts/m55_public_evidence.py",
        }
        for name, expected in manifest["public_files_sha256"].items():
            path = (args.package / name).resolve()
            assert path.is_relative_to(Path(__file__).resolve().parents[1])
            assert digest(path) == expected, name
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
