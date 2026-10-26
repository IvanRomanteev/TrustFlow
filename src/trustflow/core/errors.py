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
