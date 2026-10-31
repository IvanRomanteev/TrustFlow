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


_LABEL_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")


def _sanitize_key(key) -> str:
    """Safe published form of a mapping key.

    String keys get the same credential protection as diagnostic
    values, except bare label-like identifiers (``input_tokens``,
    ``api_key``): a label names a field, it is not a secret value, and
    its value is redacted separately when the label is credential-like.
    A label containing digits may embed an actual secret value, so it
    still goes through full redaction. Unsupported key objects are
    never stringified: arbitrary ``__str__``/``__repr__`` is untrusted.
    """
    if isinstance(key, str):
        if _LABEL_KEY.fullmatch(key) and not any(ch.isdigit() for ch in key):
            text = key
        else:
            text = redact(key)
        return text if len(text) <= _MAX_STR else text[:_MAX_STR] + "<truncated>"
    if key is None or isinstance(key, (bool, int, float)):
        return str(key)
    return "<unsupported-key>"


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
        published: dict[str, object] = {}
        collisions: dict[str, int] = {}
        for k, v in list(value.items())[:_MAX_ITEMS]:
            skey = _sanitize_key(k)
            sval = ("[REDACTED]" if isinstance(k, str)
                    and _CREDENTIAL_KEY.search(k)
                    else sanitize_payload(v, depth + 1))
            if skey in published:
                # Explicit collision-safe representation: never
                # silently overwrite a previously published entry.
                n = collisions.get(skey, 1) + 1
                collisions[skey] = n
                unique = f"{skey} <collision-{n}>"
                while unique in published:
                    n += 1
                    collisions[skey] = n
                    unique = f"{skey} <collision-{n}>"
                skey = unique
            published[skey] = sval
        return published
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= _MAX_STR else text[:_MAX_STR] + "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<unsupported>"
