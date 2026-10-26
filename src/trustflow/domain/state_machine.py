"""Validated workflow transitions."""

from trustflow.core.errors import InvalidTransitionError
from trustflow.domain.models import TERMINAL, TRANSITIONS, RunStatus, WorkflowRun


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def transition(run: WorkflowRun, target: RunStatus) -> WorkflowRun:
    """Move a run to a new status or raise InvalidTransitionError."""
    if run.status in TERMINAL and target != run.status:
        raise InvalidTransitionError(f"Run {run.run_id} is terminal ({run.status})")
    if not can_transition(run.status, target):
        raise InvalidTransitionError(f"Invalid transition {run.status} -> {target}")
    run.status = target
    from datetime import UTC, datetime

    run.updated_at = datetime.now(UTC)
    return run
