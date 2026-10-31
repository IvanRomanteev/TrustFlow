"""M4 offline evaluation, observability and metrics (fake provider only).

This package is an evaluation/telemetry layer over the existing
WorkflowEngine, AuditLogger and UsageTracker. It never executes real
transfers, never bypasses policy/approval/verification, and never
creates an alternate source of truth for workflow state: observed
outcomes, usage and traces are read from the authoritative run, ledger,
audit and llm_calls records produced by a normal engine execution.
"""

from trustflow.evaluation.cases import DATASET_VERSION, EVAL_CASES, EvalCase
from trustflow.evaluation.schemas import CaseResult, EvalReport

__all__ = ["DATASET_VERSION", "EVAL_CASES", "CaseResult", "EvalCase", "EvalReport"]
