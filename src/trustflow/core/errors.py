"""Typed application errors."""


class TrustFlowError(Exception):
    """Base error for all TrustFlow failures."""


class InvalidTransitionError(TrustFlowError):
    """Raised when a workflow status transition is not allowed."""


class ToolNotFoundError(TrustFlowError):
    """Raised when a tool name is not in the registry allowlist."""


class ToolValidationError(TrustFlowError):
    """Raised when tool arguments fail schema validation."""


class ApprovalRequiredError(TrustFlowError):
    """Raised when execution is attempted without required approval."""


class ApprovalError(TrustFlowError):
    """Raised for duplicate, stale-hash, or out-of-state approvals."""


class RetryExhaustedError(TrustFlowError):
    """Raised when a workflow exhausts its retry budget."""


class LLMNotConfiguredError(TrustFlowError):
    """Raised when a real LLM call is attempted without configuration."""


class LLMResponseError(TrustFlowError):
    """Raised for malformed or failed live model responses (fail closed).

    Carries the usage record when the provider reported one, so incurred
    cost is preserved even though the domain response was invalid.
    """

    def __init__(self, message: str, record: object | None = None,
                 records: list | None = None) -> None:
        super().__init__(message)
        # ``records`` carries every transport attempt of the failed logical
        # call; the last one is also exposed as ``record`` for callers that
        # account a single record (the tracker expands its prior attempts).
        self.records: list = list(records) if records else (
            [record] if record is not None else []
        )
        self.record = record if record is not None else (
            self.records[-1] if self.records else None
        )


class PlanValidationError(TrustFlowError):
    """Raised when a proposed plan fails allowlist/schema/precondition checks."""


class TokenBudgetExceededError(TrustFlowError):
    """Raised when a workflow exceeds its application-inference token budget."""


class ConflictingOperationError(TrustFlowError):
    """Raised when an operation identity is reused with different arguments."""


class IntentConflictError(TrustFlowError):
    """Raised when a revision changes a committed intent or is ambiguous."""


class ConcurrentUpdateError(TrustFlowError):
    """Raised when a persisted run changed since it was loaded (M3).

    Optimistic-concurrency backstop that keeps hash-bound approvals
    single-use across processes; the API maps it to HTTP 409.
    """


class EvidenceValidationError(TrustFlowError):
    """Typed mandatory-evidence failure (M2, docs/m2.md).

    Carries stable span/retrieval codes. In mandatory mode this never
    degrades to silently absent evidence: before a commit it fails the
    run before mutation; after a commit the engine routes it into
    bounded verification-only recovery.
    """

    def __init__(self, codes: list[str]) -> None:
        self.codes = list(codes)
        super().__init__(
            "Evidence validation failed: " + ",".join(sorted(set(self.codes))))
