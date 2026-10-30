"""Restart recovery (M3): interrupted runs never re-execute.

A crash can strand a run in EXECUTING/VERIFYING. On startup such runs
move to VERIFICATION_PENDING (verification-only recovery): committed
payments stay committed and are never repeated, and uncommitted
interrupted work fails closed at the attempt limit. AWAITING_APPROVAL,
VERIFICATION_PENDING and EXECUTION_UNCERTAIN keep their exact M1/M2
semantics across a restart.
"""

from trustflow.audit.logger import AuditLogger
from trustflow.core.errors import ConcurrentUpdateError
from trustflow.domain.models import RunStatus
from trustflow.domain.state_machine import transition
from trustflow.orchestration.engine import RunRepository

_RECOVERABLE = frozenset({RunStatus.EXECUTING, RunStatus.VERIFYING})
_RECOVERY_ERROR = (
    "Recovered after restart; verification-only recovery, no re-execution"
)


def recover_interrupted_runs(repo: RunRepository,
                             audit: AuditLogger) -> list[str]:
    """Move crash-stranded runs to VERIFICATION_PENDING. Returns ids."""
    recovered: list[str] = []
    for run in repo.list():
        if run.status not in _RECOVERABLE:
            continue
        if run.status == RunStatus.EXECUTING:
            transition(run, RunStatus.VERIFYING)
        transition(run, RunStatus.VERIFICATION_PENDING)
        run.error = _RECOVERY_ERROR
        try:
            repo.save(run)
        except ConcurrentUpdateError:
            continue  # another process recovered it first
        audit.log(run.run_id, "system", "restart_recovered",
                  {"recovery_state": str(RunStatus.VERIFICATION_PENDING)})
        recovered.append(run.run_id)
    return recovered
