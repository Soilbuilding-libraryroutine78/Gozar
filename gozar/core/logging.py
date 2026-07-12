"""Structured (JSON) logging with secret-safe redaction.

Two responsibilities:

1. **Structured logs.** :func:`configure_logging` installs a JSON formatter on the
   root logger so every record is a single machine-parseable line carrying the
   timestamp, level, logger name, message, and any structured ``extra`` fields
   (for example a correlation/request id). :func:`get_logger` returns a namespaced
   logger.

2. **Secret-safe redaction.** :func:`redact` walks any logged payload (dicts,
   lists, tuples, strings) and masks anything that looks like credential material
   - upstream credential secrets, subscription access/refresh tokens, client-token
   secrets, authorization headers, and API keys - before it is ever written.
   This enforces Requirement 16.4: Gozar never logs secret values.

Redaction works two ways, and is language- and locale-agnostic (no per-language
word lists, Unicode-aware): by **key name** (a mapping key whose name indicates a
secret) and by **value pattern** (a string value that matches a known secret
shape such as a Gozar client token, an ``Authorization`` header value, or a
provider API key). The :class:`SecretRedactingFilter` applies redaction to log
records automatically as a defense-in-depth backstop.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

# The placeholder substituted for any redacted value. Never reveals length or
# content of the original secret.
REDACTED = "***REDACTED***"

# Maximum recursion depth when walking a payload, to bound work on pathological
# or cyclic-looking structures.
_MAX_DEPTH = 12

# --- Key-name based redaction -------------------------------------------------
# A mapping key is redacted when its (lowercased) name contains any of these
# fragments. Substring matching keeps this robust to prefixes/suffixes such as
# ``access_token``, ``refresh_token``, ``x-api-key``, ``client_token_secret``.
_SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",  # access_token, refresh_token, client_token, subscription_token
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "credential",
    "private_key",
    "pepper",
    "ciphertext",
    "wrapped_dek",
    "nonce",
    "bearer",
    "cookie",
    "set-cookie",
    "session",
)

# Some keys contain a secret fragment but are safe, non-sensitive metadata.
# These are explicitly allowed so useful fields are not over-redacted.
_SAFE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "token_count",
        "token_counts",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "max_tokens",
        "token_id",
        "token_label",
        "token_status",
        "session_id",
        "id_prefix",
    }
)

# --- Value-pattern based redaction -------------------------------------------
# Bounded, anchored patterns for known secret shapes. These are deliberately
# simple and bounded (no catastrophic backtracking): a Gozar client token
# (``gz-<id_prefix>-<secret>``), a bearer/authorization header value, an OpenAI
# style key (``sk-...``), and an Anthropic style key (``sk-ant-...``).
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgz-[A-Za-z0-9_-]{2,}-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}\b"),
)


def _is_secret_key(key: str) -> bool:
    """Return True if a mapping key name indicates secret material."""
    lowered = key.lower()
    if lowered in _SAFE_KEY_NAMES:
        return False
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _redact_string(value: str) -> str:
    """Mask any secret-shaped substrings inside a free-form string value."""
    redacted = value
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact(payload: Any, *, _depth: int = 0) -> Any:
    """Return a copy of ``payload`` with all secret material masked.

    Recursively walks mappings, sequences, and strings:

    * mapping entries whose **key** indicates a secret are replaced with
      :data:`REDACTED` regardless of value type;
    * string **values** matching a known secret pattern are masked in place;
    * other scalars are returned unchanged.

    The original input is never mutated. Used by the logging filter and by any
    component that wants to log a structured payload safely (Requirement 16.4).
    """
    if _depth > _MAX_DEPTH:
        return REDACTED

    if isinstance(payload, dict):
        result: dict[Any, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and _is_secret_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact(value, _depth=_depth + 1)
        return result

    if isinstance(payload, (list, tuple)):
        redacted_items = [redact(item, _depth=_depth + 1) for item in payload]
        return type(payload)(redacted_items) if isinstance(payload, tuple) else redacted_items

    if isinstance(payload, str):
        return _redact_string(payload)

    return payload


# Standard ``LogRecord`` attributes that should not be emitted as structured
# ``extra`` fields (they are either handled explicitly or are internal).
_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName",
    }
)


class SecretRedactingFilter(logging.Filter):
    """Logging filter that redacts secret material from every record.

    Applied as defense-in-depth so that even ad-hoc log calls cannot leak a
    secret: the formatted message and any structured ``extra`` fields are passed
    through :func:`redact` before emission.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the rendered message text (covers f-strings/%-args already
        # interpolated as well as plain messages).
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = _redact_string(message)
        record.args = ()

        # Redact structured extras attached to the record.
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            if isinstance(key, str) and _is_secret_key(key):
                record.__dict__[key] = REDACTED
            else:
                record.__dict__[key] = redact(value)
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        # Attach any structured extras (already redacted by the filter).
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in entry:
                continue
            entry[key] = value

        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "info") -> None:
    """Configure root logging for structured JSON output with redaction.

    Idempotent: replaces existing handlers so repeated calls (for example across
    app reloads or tests) do not stack duplicate output. ``level`` accepts the
    config vocabulary (``debug | info | warn | error``).
    """
    numeric_level = _coerce_level(level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)


def _coerce_level(level: str) -> int:
    """Translate the config log-level vocabulary into a ``logging`` level."""
    mapping = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    return mapping.get(level.lower(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger (for example ``get_logger(__name__)``)."""
    return logging.getLogger(name)
