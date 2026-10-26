import pytest

from trustflow.core.errors import ToolNotFoundError, ToolValidationError
from trustflow.core.settings import Settings
from trustflow.domain.models import PlanStep, RiskLevel
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry


def test_policy_overrides_llm_suggestion():
    policy = PolicyEngine(Settings(high_risk_threshold=1000.0))
    step = PlanStep(
        step_id="s1", tool_name="simulate_transfer",
        args={"from_account": "acc_alice", "to_account": "acc_bob", "amount": 5000},
        suggested_risk=RiskLevel.LOW,
    )
    assert policy.assess_step(step) == RiskLevel.HIGH


def test_policy_read_only_is_low():
    policy = PolicyEngine(Settings())
    step = PlanStep(step_id="s1", tool_name="get_balance", args={"account_id": "acc_alice"})
    assert policy.assess_step(step) == RiskLevel.LOW


def test_unknown_tool_fails_closed():
    policy = PolicyEngine(Settings())
    step = PlanStep(step_id="s1", tool_name="wire_real_money", args={})
    assert policy.assess_step(step) == RiskLevel.HIGH


def test_registry_rejects_unknown_tool():
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.invoke("wire_real_money", {})


def test_registry_validates_args():
    registry = ToolRegistry()
    with pytest.raises(ToolValidationError):
        registry.invoke("simulate_transfer", {"from_account": "acc_alice"})


def test_transfer_idempotent_no_double_debit():
    registry = ToolRegistry()
    before = registry.ledger.total_balance()
    args = {"from_account": "acc_alice", "to_account": "acc_bob", "amount": 250.0}
    first = registry.invoke("simulate_transfer", args, idempotency_key="k-1")
    second = registry.invoke("simulate_transfer", args, idempotency_key="k-1")
    assert first.data["tx_id"] == second.data["tx_id"]
    assert registry.ledger.get_balance("acc_bob").balance == 1450.0
    assert registry.ledger.total_balance() == before


def test_tool_definitions_are_flat():
    registry = ToolRegistry()
    for definition in registry.tool_definitions():
        assert "$ref" not in str(definition)
        assert "definitions" not in str(definition)
