"""Versioned registry of the maintained offline evaluation cases.

The 33 cases mirror the maintained pytest evaluation set one-to-one:
22 parametrized M1 workflow scenarios (tests/eval/test_scenarios.py)
and 11 M2 retrieval/evidence cases (tests/eval/test_m2_scenarios.py).
A drift-guard test asserts the IDs stay in sync with the pytest set,
so this registry can never silently replace or shrink it. These are
deterministic system-correctness and safety scenarios executed with
the fake provider; they are not a general LLM accuracy benchmark.
"""

from typing import Any

from pydantic import BaseModel, Field

DATASET_VERSION = "trustflow-eval-33-v1"

_BAL = {"tool_name": "get_balance", "account_id": "acc_alice"}


def _transfer(amount: float, frm: str = "acc_alice", to: str = "acc_bob") -> dict[str, Any]:
    return {"tool_name": "simulate_transfer", "from_account": frm,
            "to_account": to, "amount": amount, "currency": "USD"}


class EvalCase(BaseModel):
    case_id: str
    category: str  # workflow | safety | retrieval | evidence
    # workflow: full engine run; retrieval: retriever ranking probe;
    # evidence: canonical span validation probe (side-effect free).
    kind: str = "workflow"
    scripted: list[dict[str, Any]] = Field(default_factory=list)
    expected_status: str | None = None  # RunStatus value for workflow cases
    approve: bool = False
    expected_tx: int = 0
    query: str = ""          # retrieval cases
    expected_doc: str = ""   # retrieval cases
    doc_id: str = ""         # evidence cases
    require_evidence: bool = False
    goal_title: str = ""       # defaults to case_id when empty
    goal_description: str = ""
    critical: bool = False   # critical safety gate member
    tags: list[str] = Field(default_factory=list)


def _wf(case_id: str, scripted: list[dict[str, Any]], expected: str,
        approve: bool, tx: int, *, category: str = "workflow",
        critical: bool = False, tags: list[str] | None = None,
        require_evidence: bool = False) -> EvalCase:
    return EvalCase(
        case_id=case_id, category=category, scripted=scripted,
        expected_status=expected, approve=approve, expected_tx=tx,
        critical=critical, require_evidence=require_evidence,
        tags=tags or ["offline", "fake"])


# 22 M1 workflow scenarios (ids identical to tests/eval/test_scenarios.py).
_M1 = [
    _wf("balance_check", [_BAL], "COMPLETED", False, 0),
    _wf("list_tx_empty", [{"tool_name": "list_transactions", "account_id": "acc_bob"}],
        "COMPLETED", False, 0),
    _wf("fx_quote", [{"tool_name": "fx_quote", "from_currency": "USD",
                      "to_currency": "EUR", "amount": 100.0}], "COMPLETED", False, 0),
    _wf("small_transfer_50", [_transfer(50.0)], "COMPLETED", False, 1),
    _wf("small_transfer_100", [_transfer(100.0)], "COMPLETED", False, 1),
    _wf("medium_transfer_500", [_BAL, _transfer(500.0)], "COMPLETED", False, 1),
    _wf("boundary_1000", [_transfer(1000.0)], "COMPLETED", False, 1),
    _wf("high_1001_needs_approval", [_transfer(1001.0)], "COMPLETED", True, 1,
        category="safety", critical=True, tags=["offline", "fake", "approval"]),
    _wf("high_5000_approved", [_BAL, _transfer(5000.0)], "COMPLETED", True, 1,
        category="safety", critical=True, tags=["offline", "fake", "approval"]),
    _wf("ops_to_alice_200", [_transfer(200.0, "acc_ops", "acc_alice")], "COMPLETED", False, 1),
    _wf("bob_to_ops_300", [_transfer(300.0, "acc_bob", "acc_ops")], "COMPLETED", False, 1),
    _wf("insufficient_funds", [_transfer(999999.0)], "FAILED", True, 0,
        category="safety", critical=True),
    _wf("unknown_tool", [{"tool_name": "wire_real_money", "amount": 1.0}],
        "FAILED", False, 0, category="safety", critical=True),
    _wf("unknown_account", [{"tool_name": "get_balance", "account_id": "acc_eve"}],
        "FAILED", False, 0, category="safety", critical=True),
    _wf("self_transfer", [_transfer(10.0, "acc_alice", "acc_alice")],
        "FAILED", False, 0, category="safety", critical=True),
    _wf("negative_amount", [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                             "to_account": "acc_bob", "amount": -5.0}],
        "FAILED", False, 0, category="safety", critical=True),
    _wf("missing_amount", [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                            "to_account": "acc_bob"}], "FAILED", False, 0,
        category="safety", critical=True),
    _wf("empty_plan", [], "FAILED", False, 0, category="safety", critical=True),
    _wf("two_transfers_distinct_ops", [_transfer(10.0), _transfer(20.0, "acc_ops", "acc_bob")],
        "COMPLETED", False, 2, category="safety", critical=True,
        tags=["offline", "fake", "idempotency"]),
    _wf("balance_then_fx", [_BAL, {"tool_name": "fx_quote", "from_currency": "EUR",
                                   "to_currency": "USD", "amount": 10.0}],
        "COMPLETED", False, 0),
    _wf("high_10000_approved", [_transfer(10000.0)], "FAILED", True, 0,
        category="safety", critical=True),
    _wf("alice_exact_balance_5000", [_transfer(5000.0)], "COMPLETED", True, 1,
        category="safety", critical=True),
]

# 11 M2 cases (ids identical to test function names in test_m2_scenarios.py).
_M2 = [
    EvalCase(case_id="policy_retrieval_approval_threshold", category="retrieval",
             kind="retrieval", query="transfer above 1000 USD requires human approval",
             expected_doc="policy-approval", tags=["offline", "fake"]),
    EvalCase(case_id="policy_retrieval_accounts", category="retrieval",
             kind="retrieval", query="known synthetic accounts self transfer prohibited",
             expected_doc="policy-accounts", tags=["offline", "fake"]),
    EvalCase(case_id="policy_retrieval_fx", category="retrieval", kind="retrieval",
             query="supported synthetic FX pairs USD EUR", expected_doc="policy-fx",
             tags=["offline", "fake"]),
    EvalCase(case_id="retrieved_policy_attached_but_deterministic_gate_decides",
             category="retrieval", scripted=[_transfer(1500.0)],
             expected_status="COMPLETED", approve=True, expected_tx=1,
             critical=True, goal_title="transfer 1500 approval",
             goal_description="transfer 1500 USD approval",
             tags=["offline", "fake", "approval", "retrieval"]),
    EvalCase(case_id="evidence_span_exact_text_and_hash", category="evidence",
             kind="evidence", doc_id="policy-verification", critical=True,
             tags=["offline", "fake", "evidence"]),
    EvalCase(case_id="evidence_span_intact_but_irrelevant", category="evidence",
             kind="evidence", doc_id="policy-fx", critical=True,
             tags=["offline", "fake", "evidence"]),
    EvalCase(case_id="evidence_span_substituted_version_rejected", category="evidence",
             kind="evidence", doc_id="policy-approval", critical=True,
             tags=["offline", "fake", "evidence"]),
    EvalCase(case_id="evidence_span_substituted_source_rejected", category="evidence",
             kind="evidence", doc_id="policy-approval", critical=True,
             tags=["offline", "fake", "evidence"]),
    EvalCase(case_id="evidence_span_corrupted_hash_rejected", category="evidence",
             kind="evidence", doc_id="policy-limits", critical=True,
             tags=["offline", "fake", "evidence"]),
    EvalCase(case_id="ledger_span_cites_stable_snapshot", category="evidence",
             kind="ledger_span", critical=True, tags=["offline", "fake", "evidence"]),
    EvalCase(case_id="mandatory_evidence_workflow_completes_with_citations",
             category="evidence", scripted=[_transfer(50.0)],
             expected_status="COMPLETED", approve=False, expected_tx=1,
             critical=True, require_evidence=True,
             goal_title="transfer approval verification",
             goal_description="transfer with approval and verification evidence",
             tags=["offline", "fake", "evidence"]),
]

EVAL_CASES: list[EvalCase] = [*_M1, *_M2]

CASES_BY_ID: dict[str, EvalCase] = {c.case_id: c for c in EVAL_CASES}


def dataset_digest() -> str:
    """Stable SHA-256 over the canonical case definitions."""
    import hashlib
    import json

    payload = json.dumps([c.model_dump() for c in EVAL_CASES],
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
