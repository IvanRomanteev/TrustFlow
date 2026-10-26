import pytest

from trustflow.core.errors import InvalidTransitionError
from trustflow.domain.models import RunStatus, WorkflowGoal, WorkflowRun
from trustflow.domain.state_machine import transition


def _run() -> WorkflowRun:
    return WorkflowRun(goal=WorkflowGoal(title="t"))


def test_valid_transition_path():
    run = _run()
    transition(run, RunStatus.PLANNING)
    transition(run, RunStatus.PLANNED)
    transition(run, RunStatus.EXECUTING)
    assert run.status == RunStatus.EXECUTING


def test_invalid_transition_rejected():
    run = _run()
    with pytest.raises(InvalidTransitionError):
        transition(run, RunStatus.COMPLETED)


def test_terminal_run_cannot_transition():
    run = _run()
    transition(run, RunStatus.PLANNING)
    transition(run, RunStatus.PLANNED)
    transition(run, RunStatus.EXECUTING)
    transition(run, RunStatus.VERIFYING)
    transition(run, RunStatus.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        transition(run, RunStatus.PLANNING)
