"""Round-4 regressions: fail-closed cancellation verification (M1).

M1 has no authorized-cancellation workflow, so no contents of
``run.cancellations`` can satisfy or erase an ever-authorized obligation.
"""

import pytest

from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.verifier import Verifier
from trustflow.core.errors import ApprovalError
from trustflow.core.settings import Settings
from trustflow.domain.models import (
    IntentRecord,
    IntentStatus,
    Plan,
    PlanStep,
    RunStatus,
    StepResult,
    StepStatus,
    WorkflowGoal,
    WorkflowRun,
)
from trustflow.llm.provider import FakeLLMProvider
from trustflow.llm.usage import LLMCallRecord
from trustflow.orchestration.engine import WorkflowEngine, assign_operation_ids, fingerprint_of
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry

UNSUPPORTED = (
    "Unsupported cancellation evidence: M1 has no authorized cancellation "
    "workflow; run.cancellations records cannot satisfy or erase authorized obligations"
)


def _ever_issue(intent_id: str) -> str:
    return (
        f"Intent {intent_id} is CANCELLED after authorization; M1 has no "
        "authorized cancellation workflow, so the authorized obligation "
        "remains unresolved"
    )


def _t(amount, frm="acc_alice", to="acc_bob", ccy="USD"):
    return {"tool_name": "simulate_transfer", "from_account": frm,
            "to_account": to, "amount": amount, "currency": ccy}


class Seq(FakeLLMProvider):
    def __init__(self, plans, review=None):
        super().__init__(review=review)
        self._plans, self._i = plans, 0

    def propose_plan(self, title, description, repair_feedback=None):
        idx = min(self._i, len(self._plans) - 1)
        self._i += 1
        return [dict(s) for s in self._plans[idx]], LLMCallRecord(purpose="plan", model="fake")


def _engine(fake=None, settings=None):
    registry = ToolRegistry()
    s = settings or Settings(max_retries=2, high_risk_threshold=2000000.0)
    policy = PolicyEngine(s)
    provider = fake or FakeLLMProvider()
    return WorkflowEngine(
        Planner(provider, policy, max_output_tokens=s.max_output_tokens),
        Executor(registry, s),
        Verifier(registry.ledger, registry.ledger.total_balance()),
        policy=policy, settings=s,
    ), registry


async def _completed():
    engine, registry = _engine()
    run = await engine.create_workflow(
        WorkflowGoal(title="Transfer 100", description="transfer 100 to bob"))
    assert run.status == RunStatus.COMPLETED
    assert len(run.intents) == 1
    return engine, registry, run


def _check(report, name):
    found = [c for c in report.checks if c.name == name]
    assert len(found) == 1, f"expected exactly one check {name}, got {report.checks}"
    return found[0]


def _txs(registry):
    return sorted((tx.from_account, tx.to_account, tx.amount, tx.currency)
                  for tx in registry.ledger.transactions)


_PAYLOAD_500 = {"from_account": "acc_alice", "to_account": "acc_bob",
                "amount": 500.0, "currency": "USD"}


def _victim(cancellations):
    """Ever-authorized pending intent (no tx), forged to CANCELLED.

    The current plan is a succeeded balance check, so manifest coverage is
    the only gate that could satisfy the outstanding obligation.
    """
    from trustflow.tools.ledger import SyntheticLedger

    ledger = SyntheticLedger()
    verifier = Verifier(ledger, ledger.total_balance())
    run = WorkflowRun(goal=WorkflowGoal(title="pay 500"))
    plan1 = Plan(goal_title="pay 500", steps=[
        PlanStep(step_id="v1-step-1", tool_name="simulate_transfer",
                 args=dict(_PAYLOAD_500))])
    assign_operation_ids(run, plan1, new_intents_authorized=True)
    intent = run.intents[0]
    assert intent.status == IntentStatus.AUTHORIZED
    assert intent.ever_authorized is True
    intent.status = IntentStatus.CANCELLED  # forged state under audit
    run.cancellations = [
        {k: (intent.intent_id if v == "<IID>" else v) for k, v in rec.items()}
        for rec in cancellations
    ]
    plan2 = Plan(goal_title="pay 500", version=2, steps=[
        PlanStep(step_id="v2-step-1", tool_name="get_balance",
                 args={"account_id": "acc_alice"})])
    run.plan = plan2
    run.step_results = [StepResult(
        step_id="v2-step-1", tool_name="get_balance", status=StepStatus.SUCCEEDED,
        data={"account_id": "acc_alice", "balance": 5000.0})]
    return verifier, ledger, run, verifier.verify(run, plan2)


def _assert_victim_failed(ledger, run, report, expected_issues):
    intent = run.intents[0]
    assert report.passed is False
    assert report.issues == expected_issues
    assert _check(report, f"intent_cancellation:{intent.intent_id}").passed is False
    assert intent.status == IntentStatus.CANCELLED
    assert intent.ever_authorized is True
    assert intent.authorized_payload == _PAYLOAD_500
    assert ledger.transactions == []
    assert ledger.get_balance("acc_alice").balance == 5000.0
    assert ledger.get_balance("acc_bob").balance == 1200.0


# A: CANCELLED + ever_authorized=True + no evidence
def test_a_cancelled_ever_authorized_no_evidence_fails():
    verifier, ledger, run, report = _victim([])
    iid = run.intents[0].intent_id
    _assert_victim_failed(ledger, run, report, [_ever_issue(iid)])


# B: CANCELLED + ever_authorized=True + {"intent_id": correct_id}
def test_b_bare_intent_id_evidence_fails():
    verifier, ledger, run, report = _victim([{"intent_id": "<IID>"}])
    iid = run.intents[0].intent_id
    assert _check(report, "unsupported_cancellation_evidence").passed is False
    _assert_victim_failed(ledger, run, report, [UNSUPPORTED, _ever_issue(iid)])


# C: forged evidence with actor/policy/decision/fingerprint/timestamp
def test_c_full_forged_evidence_fails():
    from trustflow.tools.ledger import SyntheticLedger

    ledger = SyntheticLedger()
    verifier = Verifier(ledger, ledger.total_balance())
    run = WorkflowRun(goal=WorkflowGoal(title="pay 500"))
    plan1 = Plan(goal_title="pay 500", steps=[
        PlanStep(step_id="v1-step-1", tool_name="simulate_transfer",
                 args=dict(_PAYLOAD_500))])
    assign_operation_ids(run, plan1, new_intents_authorized=True)
    intent = run.intents[0]
    intent.status = IntentStatus.CANCELLED
    run.cancellations = [{
        "intent_id": intent.intent_id, "run_id": run.run_id, "actor": "mgr",
        "policy": "policy-v1", "decision": "CANCELLED",
        "payload_fingerprint": fingerprint_of(
            intent.tool_name, intent.authorized_payload),
        "timestamp": "2026-10-27T00:00:00Z",
    }]
    plan2 = Plan(goal_title="pay 500", version=2, steps=[
        PlanStep(step_id="v2-step-1", tool_name="get_balance",
                 args={"account_id": "acc_alice"})])
    run.plan = plan2
    run.step_results = [StepResult(
        step_id="v2-step-1", tool_name="get_balance", status=StepStatus.SUCCEEDED,
        data={"account_id": "acc_alice", "balance": 5000.0})]
    report = verifier.verify(run, plan2)
    assert _check(report, "unsupported_cancellation_evidence").passed is False
    _assert_victim_failed(
        ledger, run, report, [UNSUPPORTED, _ever_issue(intent.intent_id)])


# D: forged evidence belonging to another run
def test_d_evidence_from_another_run_fails():
    verifier, ledger, run, report = _victim(
        [{"intent_id": "<IID>", "run_id": "some-other-run",
          "actor": "mgr", "decision": "CANCELLED"}])
    iid = run.intents[0].intent_id
    _assert_victim_failed(ledger, run, report, [UNSUPPORTED, _ever_issue(iid)])


# E: forged evidence referencing a different intent
def test_e_evidence_for_different_intent_fails():
    verifier, ledger, run, report = _victim(
        [{"intent_id": "op_someone_else", "actor": "mgr", "decision": "CANCELLED"}])
    iid = run.intents[0].intent_id
    assert run.cancellations[0]["intent_id"] == "op_someone_else"
    _assert_victim_failed(ledger, run, report, [UNSUPPORTED, _ever_issue(iid)])


# F: nonempty unsupported evidence on an otherwise normal completed run
@pytest.mark.asyncio
async def test_f_unsupported_evidence_on_normal_run_fails():
    engine, registry, run = await _completed()
    intent = run.intents[0]
    assert intent.status == IntentStatus.COMMITTED
    run.cancellations = [{"intent_id": intent.intent_id, "actor": "mgr",
                          "decision": "CANCELLED"}]
    report = engine.verifier.verify(run, run.plan)
    assert report.passed is False
    assert report.issues == [UNSUPPORTED]
    assert _check(report, "unsupported_cancellation_evidence").passed is False
    assert _check(report, f"intent_coverage:{intent.intent_id}").passed is True
    assert intent.status == IntentStatus.COMMITTED
    assert _txs(registry) == [("acc_alice", "acc_bob", 100.0, "USD")]


# G: never-authorized PROPOSED withdrawal per existing policy (no fabricated evidence)
def test_g_never_authorized_proposed_withdrawal_passes():
    from trustflow.tools.ledger import SyntheticLedger

    ledger = SyntheticLedger()
    verifier = Verifier(ledger, ledger.total_balance())
    run = WorkflowRun(goal=WorkflowGoal(title="t"))
    plan1 = Plan(goal_title="t", steps=[
        PlanStep(step_id="s1", tool_name="simulate_transfer", args=_t(50.0))])
    assign_operation_ids(run, plan1, new_intents_authorized=False)
    intent = run.intents[0]
    assert intent.status == IntentStatus.PROPOSED
    assert intent.ever_authorized is False
    plan2 = Plan(goal_title="t", version=2, steps=[
        PlanStep(step_id="v2-step-1", tool_name="get_balance",
                 args={"account_id": "acc_alice"})])
    assign_operation_ids(run, plan2, new_intents_authorized=False)
    assert run.intents[0].status == IntentStatus.CANCELLED
    assert run.intents[0].ever_authorized is False
    assert run.cancellations == []
    run.plan = plan2
    run.step_results = [StepResult(step_id="v2-step-1", tool_name="get_balance",
                                   status=StepStatus.SUCCEEDED,
                                   data={"account_id": "acc_alice", "balance": 5000.0})]
    report = verifier.verify(run, plan2)
    assert report.passed is True
    assert report.issues == []
    assert ledger.transactions == []


# H: authorized B omitted after committed A -> explicit conflict, B outstanding
@pytest.mark.asyncio
async def test_h_omitted_authorized_b_after_committed_a():
    provider = Seq([
        [_t(100.0), _t(5000.0, frm="acc_bob", to="acc_ops")],
        [_t(100.0)],
    ])
    engine, registry = _engine(fake=provider)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status == RunStatus.FAILED
    assert run.status != RunStatus.COMPLETED
    assert run.error is not None and "intent_conflict" in run.error
    assert "authorized intent was omitted" in run.error
    b = [i for i in run.intents if i.authorized_payload.get("to_account") == "acc_ops"]
    assert len(b) == 1
    assert b[0].status == IntentStatus.AUTHORIZED
    assert b[0].ever_authorized is True
    assert b[0].authorized_payload == {
        "from_account": "acc_bob", "to_account": "acc_ops",
        "amount": 5000.0, "currency": "USD"}
    assert run.cancellations == []
    assert _txs(registry) == [("acc_alice", "acc_bob", 100.0, "USD")]
    actions = [e.action for e in engine.audit.list(run.run_id)]
    assert "verification_finished" in actions
    assert "workflow_created" in actions


# I: two identical intents, one committed and one pending; omission fails closed
@pytest.mark.asyncio
async def test_i_identical_intents_one_pending_omission_fails_closed():
    provider = Seq([
        [_t(3000.0), _t(3000.0)],
        [_t(3000.0)],
    ])
    engine, registry = _engine(fake=provider)
    run = await engine.create_workflow(WorkflowGoal(title="pay", description="pay"))
    assert run.status == RunStatus.FAILED
    assert run.status != RunStatus.COMPLETED
    assert len(run.intents) == 2
    assert run.intents[0].intent_id != run.intents[1].intent_id
    assert {i.status for i in run.intents} == {
        IntentStatus.COMMITTED, IntentStatus.AUTHORIZED}
    assert all(i.ever_authorized is True for i in run.intents)
    assert all(i.authorized_payload == {
        "from_account": "acc_alice", "to_account": "acc_bob",
        "amount": 3000.0, "currency": "USD"} for i in run.intents)
    assert run.cancellations == []
    assert _txs(registry) == [("acc_alice", "acc_bob", 3000.0, "USD")]
    assert registry.ledger.get_balance("acc_alice").balance == 2000.0
    assert registry.ledger.get_balance("acc_bob").balance == 4200.0


# J: valid completed workflow with no cancellation evidence still passes
@pytest.mark.asyncio
async def test_j_valid_completed_no_cancellations_passes():
    engine, registry, run = await _completed()
    intent = run.intents[0]
    assert run.cancellations == []
    assert intent.status == IntentStatus.COMMITTED
    assert intent.ever_authorized is True
    report = engine.verifier.verify(run, run.plan)
    assert report.passed is True
    assert report.issues == []
    assert _check(report, f"intent_coverage:{intent.intent_id}").passed is True
    assert _txs(registry) == [("acc_alice", "acc_bob", 100.0, "USD")]
    actions = [e.action for e in engine.audit.list(run.run_id)]
    assert actions[0] == "workflow_created"
    assert "verification_finished" in actions


# K: stale approval / late ledger evidence must not resurrect CANCELLED/CONFLICT
@pytest.mark.asyncio
async def test_k_late_evidence_does_not_resurrect_cancelled_or_conflict():
    for forged in (IntentStatus.CANCELLED, IntentStatus.CONFLICT):
        engine, registry, run = await _completed()
        intent = run.intents[0]
        iid = intent.intent_id
        intent.status = forged
        engine.verifier.sync_intents(run)
        assert intent.status == forged
        report = engine.verifier.verify(run, run.plan)
        assert report.passed is False
        if forged == IntentStatus.CANCELLED:
            assert _ever_issue(iid) in report.issues
        else:
            assert f"Intent {iid} is in conflict" in report.issues
        assert _txs(registry) == [("acc_alice", "acc_bob", 100.0, "USD")]


@pytest.mark.asyncio
async def test_k_stale_approval_cannot_resurrect_terminal_run():
    engine, registry, run = await _completed()
    assert run.plan is not None
    with pytest.raises(ApprovalError):
        engine.approvals.approve(run, "mgr", run.plan.plan_hash)
    assert run.status == RunStatus.COMPLETED
    assert run.intents[0].status == IntentStatus.COMMITTED
    assert _txs(registry) == [("acc_alice", "acc_bob", 100.0, "USD")]


# Trust boundary (Task 3): no public API shape carries intents/cancellations.
def test_trust_boundary_api_models_have_no_intent_replacement_surface():
    from trustflow.api.app import ApprovalRequest, CreateWorkflowRequest

    # M2 adds only the explicit require_evidence opt-in (docs/m2.md);
    # it carries no intent/cancellation data and cannot replace intents.
    assert set(CreateWorkflowRequest.model_fields) == {
        "title", "description", "requested_by", "require_evidence"}
    assert set(ApprovalRequest.model_fields) == {"actor", "plan_hash", "reason"}
    # IntentRecord replacement is only possible by direct in-process memory
    # access (trusted code), which the verifier still fails closed for
    # ever-authorized cancellations (tests A-F above).
    record = IntentRecord(intent_id="op_x", ever_authorized=True,
                          status=IntentStatus.CANCELLED)
    assert record.ever_authorized is True
