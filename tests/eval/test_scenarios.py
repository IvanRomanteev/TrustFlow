"""Synthetic evaluation scenarios (M1: 22 of the M4 target of 30+).

All data is synthetic. Policy-retrieval and evidence-span scenario families
are explicitly deferred to M2 (see docs/m1.md); M1 covers planning,
approval, execution, verification, idempotency and recovery behaviour.
"""

import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.core.settings import Settings
from trustflow.domain.models import RunStatus, WorkflowGoal
from trustflow.llm.provider import FakeLLMProvider
from trustflow.orchestration.engine import WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry

BAL = {"tool_name": "get_balance", "account_id": "acc_alice"}


def _transfer(amount, frm="acc_alice", to="acc_bob"):
    return {"tool_name": "simulate_transfer", "from_account": frm,
            "to_account": to, "amount": amount, "currency": "USD"}


# (name, scripted_steps, expected_status, approve_if_needed, expected_tx_count)
SCENARIOS = [
    ("balance_check", [BAL], RunStatus.COMPLETED, False, 0),
    ("list_tx_empty", [{"tool_name": "list_transactions", "account_id": "acc_bob"}],
     RunStatus.COMPLETED, False, 0),
    ("fx_quote", [{"tool_name": "fx_quote", "from_currency": "USD",
                   "to_currency": "EUR", "amount": 100.0}], RunStatus.COMPLETED, False, 0),
    ("small_transfer_50", [_transfer(50.0)], RunStatus.COMPLETED, False, 1),
    ("small_transfer_100", [_transfer(100.0)], RunStatus.COMPLETED, False, 1),
    ("medium_transfer_500", [BAL, _transfer(500.0)], RunStatus.COMPLETED, False, 1),
    ("boundary_1000", [_transfer(1000.0)], RunStatus.COMPLETED, False, 1),
    ("high_1001_needs_approval", [_transfer(1001.0)], RunStatus.COMPLETED, True, 1),
    ("high_5000_approved", [BAL, _transfer(5000.0)], RunStatus.COMPLETED, True, 1),
    ("ops_to_alice_200", [_transfer(200.0, "acc_ops", "acc_alice")],
     RunStatus.COMPLETED, False, 1),
    ("bob_to_ops_300", [_transfer(300.0, "acc_bob", "acc_ops")],
     RunStatus.COMPLETED, False, 1),
    ("insufficient_funds", [_transfer(999999.0)], RunStatus.FAILED, True, 0),
    ("unknown_tool", [{"tool_name": "wire_real_money", "amount": 1.0}],
     RunStatus.FAILED, False, 0),
    ("unknown_account", [{"tool_name": "get_balance", "account_id": "acc_eve"}],
     RunStatus.FAILED, False, 0),
    ("self_transfer", [_transfer(10.0, "acc_alice", "acc_alice")],
     RunStatus.FAILED, False, 0),
    ("negative_amount", [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                          "to_account": "acc_bob", "amount": -5.0}],
     RunStatus.FAILED, False, 0),
    ("missing_amount", [{"tool_name": "simulate_transfer", "from_account": "acc_alice",
                         "to_account": "acc_bob"}], RunStatus.FAILED, False, 0),
    ("empty_plan", [], RunStatus.FAILED, False, 0),
    ("two_transfers_distinct_ops", [_transfer(10.0), _transfer(20.0, "acc_ops", "acc_bob")],
     RunStatus.COMPLETED, False, 2),
    ("balance_then_fx", [BAL, {"tool_name": "fx_quote", "from_currency": "EUR",
                               "to_currency": "USD", "amount": 10.0}],
     RunStatus.COMPLETED, False, 0),
    ("high_10000_approved", [_transfer(10000.0)], RunStatus.FAILED, True, 0),
    ("alice_exact_balance_5000", [_transfer(5000.0)], RunStatus.COMPLETED, True, 1),
]


@pytest.mark.parametrize(
    "name,steps,expected,approve,tx_count",
    SCENARIOS,
    ids=[s[0] for s in SCENARIOS],
)
@pytest.mark.asyncio
async def test_scenario(name, steps, expected, approve, tx_count):
    registry = ToolRegistry()
    settings = Settings(max_retries=1)
    policy = PolicyEngine(settings)
    fake = FakeLLMProvider(scripted=steps)
    engine = WorkflowEngine(
        Planner(fake, policy), Executor(registry, settings),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=settings,
        semantic=SemanticReviewer(fake, registry.ledger),
    )
    run = await engine.create_workflow(WorkflowGoal(title=name, description=name))
    for _ in range(4):
        if run.status != RunStatus.AWAITING_APPROVAL:
            break
        assert approve, f"{name} unexpectedly required approval"
        engine.approvals.approve(run, "eval", run.plan.plan_hash)
        run = await engine.resume_after_approval(run)
    assert run.status == expected, f"{name}: got {run.status} ({run.error})"
    assert len(registry.ledger.transactions) == tx_count, name
