"""Safe, bounded diagnostic representation for M4 harness errors (F4).

Raw exception messages are untrusted text: they can embed credentials,
approval tokens, Authorization headers or secret-bearing paths. The M4
reporting boundary therefore publishes only the exception class name
plus a redacted, length-bounded message produced by the existing
``sanitize_error`` helper. Report writers redact again defensively.
"""

import re

from trustflow.core.logging import redact, sanitize_error

_MAX_DETAIL = 160
_MAX_STR = 2000
_MAX_ITEMS = 100
_MAX_DEPTH = 8

# Mapping keys whose values are credentials by construction. Deliberately
# narrower than a substring "token" match: usage fields such as
# input_tokens/total_tokens are measurements, not secrets, and must
# survive sanitization unchanged.
_CREDENTIAL_KEY = re.compile(
    r"(api[_-]?key|authorization|password|secret|approval[_-]?token|"
    r"access[_-]?token|auth[_-]?token|credentials?)",
    re.IGNORECASE)


def safe_error_detail(exc: BaseException) -> str:
    """Bounded, redacted one-line description of an exception."""
    message = sanitize_error(str(exc))[:_MAX_DETAIL]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def safe_text(text: str) -> str:
    """Defensive redaction for any string crossing into a report."""
    return redact(str(text))[:1000]


def sanitize_payload(value, depth: int = 0):
    """Sanitized, bounded presentation copy of arbitrary report data.

    Applied at the publication boundary to every structured payload
    (results, traces, manifest, ablations, completion metadata), so
    no producer is trusted to have sanitized upstream. Credential keys
    and secret-looking strings are redacted recursively; strings,
    collections and depth are bounded; unsupported custom objects are
    replaced by a fixed placeholder instead of their arbitrary
    ``str()`` representation. JSON scalars pass through unchanged, so
    verdicts, counts, usage and costs are never altered.
    """
    if depth > _MAX_DEPTH:
        return "<truncated: depth>"
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if _CREDENTIAL_KEY.search(str(k))
            else sanitize_payload(v, depth + 1)
            for k, v in list(value.items())[:_MAX_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= _MAX_STR else text[:_MAX_STR] + "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<unsupported>"
