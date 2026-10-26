"""Structured logging with recursive secret redaction."""

import logging
import re
from typing import Any

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)")
_KV_SECRET = re.compile(
    r"([A-Za-z_]*(?:api[_-]?key|token|secret|password)[A-Za-z_]*\s*[:=]\s*)\S+",
    re.IGNORECASE,
)
# Whitespace-delimited credential forms: "Authorization <v>",
# "secret <v>", "credentials are <v>", "token: <v>".
_WS_SECRET = re.compile(
    r"((?:authorization|credentials?|api[_-]?key|token|secret|password)"
    r"(?:\s+(?:is|are))?\s*[:=]?\s+)(?:\S*[0-9\-]\S*|\S{12,})",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Remove secret-looking values from a string."""
    out = _SECRET_VALUE.sub("[REDACTED]", text)
    out = _KV_SECRET.sub(r"\1[REDACTED]", out)
    return _WS_SECRET.sub(r"\1[REDACTED]", out)


def redact_any(value: Any) -> Any:
    """Recursively sanitize dicts, lists, tuples and strings. Never mutates."""
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if _SECRET_KEY.search(str(key)) else redact_any(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_any(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def redact_mapping(data: dict) -> dict:
    """Backwards-compatible wrapper over redact_any for dict payloads."""
    cleaned: dict = redact_any(data)
    return cleaned


def sanitize_error(text: str) -> str:
    return redact(str(text))[:500]


def get_logger(name: str = "trustflow") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
