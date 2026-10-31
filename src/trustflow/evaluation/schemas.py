"""Versioned machine-readable evaluation result schemas (schema 1.0).

Null means unknown/not measured; zero means measured zero. Token usage
is never invented: fake-provider calls report unknown tokens (None).
Timestamps and run ids are traceability metadata only and are excluded
from verdict-reproducibility comparisons.
"""

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class CaseUsage(BaseModel):
    logical_calls: int = 0
    transport_attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    # True when at least one call reported no token usage at all.
    usage_unknown: bool = False
    provider: str = "fake"
    model: str | None = None


class CaseResult(BaseModel):
    case_id: str
    category: str
    # PASS | FAIL (assertion mismatch) | ERROR (harness/infrastructure)
    status: str
    expected_outcome: str | None = None
    observed_outcome: str | None = None
    duration_ms: float = 0.0
    workflow_run_id: str | None = None
    tx_count: int | None = None
    failure_category: str | None = None
    safety_violations: list[str] = Field(default_factory=list)
    usage: CaseUsage = Field(default_factory=CaseUsage)
    estimated_cost_usd: float | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    trace_ref: str | None = None
    detail: str = ""

    def verdict_key(self) -> tuple:
        """Fields that must be identical across reproducible reruns."""
        return (self.case_id, self.status, self.expected_outcome,
                self.observed_outcome, tuple(self.safety_violations),
                self.tx_count, self.failure_category)


class RunMetadata(BaseModel):
    git_sha: str = ""
    git_dirty: bool = False
    dataset_version: str = ""
    dataset_digest: str = ""
    config_digest: str = ""
    seed: int = 42
    provider_mode: str = "fake"
    model: str | None = None
    python_version: str = ""
    harness: str = "trustflow.evaluation"
    started_at: str = ""  # traceability only; excluded from comparisons


class EvalSummary(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    critical_total: int = 0
    critical_passed: int = 0
    safety_violation_count: int = 0
    expected_policy_rejections: int = 0
    completed_workflows: int = 0
    blocked_or_failed_workflows: int = 0


class EvalReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_metadata: RunMetadata = Field(default_factory=RunMetadata)
    cases: list[CaseResult] = Field(default_factory=list)
    summary: EvalSummary = Field(default_factory=EvalSummary)
    metrics: dict = Field(default_factory=dict)

    def verdict_digest(self) -> str:
        """Digest of reproducible verdict content (no timestamps/ids)."""
        import hashlib
        import json

        payload = json.dumps(
            [list(c.verdict_key()) for c in self.cases]
            + [self.summary.model_dump()],
            sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()
