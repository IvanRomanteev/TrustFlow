"""Structured logging with secret redaction."""

import logging
import re

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)")


def redact(text: str) -> str:
    """Remove secret-looking values from a log line."""
    return _SECRET_VALUE.sub("[REDACTED]", text)


def redact_mapping(data: dict) -> dict:
    """Return a copy with secret keys masked. Never mutates the input."""
    out: dict = {}
    for key, value in data.items():
        if _SECRET_KEY.search(str(key)):
            out[key] = "[REDACTED]"
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, str):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


def get_logger(name: str = "trustflow") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
