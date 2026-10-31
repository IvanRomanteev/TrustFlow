"""Safe, bounded diagnostic representation for M4 harness errors (F4).

Raw exception messages are untrusted text: they can embed credentials,
approval tokens, Authorization headers or secret-bearing paths. The M4
reporting boundary therefore publishes only the exception class name
plus a redacted, length-bounded message produced by the existing
``sanitize_error`` helper. Report writers redact again defensively.
"""

from trustflow.core.logging import redact, sanitize_error

_MAX_DETAIL = 160


def safe_error_detail(exc: BaseException) -> str:
    """Bounded, redacted one-line description of an exception."""
    message = sanitize_error(str(exc))[:_MAX_DETAIL]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def safe_text(text: str) -> str:
    """Defensive redaction for any string crossing into a report."""
    return redact(str(text))[:1000]
