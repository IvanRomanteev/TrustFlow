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

    def __init__(self, message: str, record: object | None = None) -> None:
        super().__init__(message)
        self.record = record


class PlanValidationError(TrustFlowError):
    """Raised when a proposed plan fails allowlist/schema/precondition checks."""


class TokenBudgetExceededError(TrustFlowError):
    """Raised when a workflow exceeds its application-inference token budget."""


class ConflictingOperationError(TrustFlowError):
    """Raised when an operation identity is reused with different arguments."""
