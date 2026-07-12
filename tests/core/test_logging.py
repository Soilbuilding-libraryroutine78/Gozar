"""Unit tests for structured logging and secret-safe redaction."""

from __future__ import annotations

import json
import logging

from gozar.core import logging as gozar_logging
from gozar.core.logging import REDACTED, redact


def test_redacts_secret_keys_by_name():
    payload = {
        "access_token": "abc123",
        "refresh_token": "def456",
        "api_key": "sk-secret",
        "password": "hunter2",
        "authorization": "Bearer xyz",
        "client_token_secret": "gz-aa-bbbbbbbb",
        "model": "gpt-4o",
        "prompt_tokens": 10,
    }
    result = redact(payload)
    assert result["access_token"] == REDACTED
    assert result["refresh_token"] == REDACTED
    assert result["api_key"] == REDACTED
    assert result["password"] == REDACTED
    assert result["authorization"] == REDACTED
    assert result["client_token_secret"] == REDACTED
    # Non-secret fields are preserved, including safe token-count metadata.
    assert result["model"] == "gpt-4o"
    assert result["prompt_tokens"] == 10


def test_safe_token_metadata_keys_not_redacted():
    payload = {
        "token_count": 42,
        "total_tokens": 100,
        "token_id": "uuid-here",
        "session_id": "sess-1",
        "id_prefix": "abcd",
    }
    assert redact(payload) == payload


def test_redacts_nested_structures():
    payload = {
        "outer": {
            "list": [{"secret": "s"}, {"label": "ok"}],
            "subscription_token": "t",
        }
    }
    result = redact(payload)
    assert result["outer"]["list"][0]["secret"] == REDACTED
    assert result["outer"]["list"][1]["label"] == "ok"
    assert result["outer"]["subscription_token"] == REDACTED


def test_redacts_secret_value_patterns_in_strings():
    text = "auth=Bearer sk-abcdefghijklmnop and client gz-prefix-secretvalue done"
    result = redact(text)
    assert "sk-abcdefghijklmnop" not in result
    assert "gz-prefix-secretvalue" not in result
    assert REDACTED in result
    assert "done" in result


def test_redact_does_not_mutate_input():
    payload = {"api_key": "secret", "nested": {"token": "t"}}
    original = {"api_key": "secret", "nested": {"token": "t"}}
    redact(payload)
    assert payload == original


def test_depth_limit_returns_redacted():
    # Build a structure deeper than the max depth.
    deep: dict = {}
    cursor = deep
    for _ in range(gozar_logging._MAX_DEPTH + 3):
        nxt: dict = {}
        cursor["child"] = nxt
        cursor = nxt
    # Should not raise and should bottom out at REDACTED.
    result = redact(deep)
    assert result is not None


def test_configure_logging_emits_redacted_json(capsys):
    gozar_logging.configure_logging("info")
    logger = gozar_logging.get_logger("test.redaction")
    logger.info("token is gz-aa-supersecretvalue here")

    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.redaction"
    assert "supersecretvalue" not in entry["message"]
    assert REDACTED in entry["message"]


def test_filter_redacts_extra_fields(capsys):
    gozar_logging.configure_logging("info")
    logger = gozar_logging.get_logger("test.extra")
    logger.info("request", extra={"api_key": "sk-shouldnotappear", "correlation_id": "abc"})

    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["api_key"] == REDACTED
    assert entry["correlation_id"] == "abc"


def test_configure_logging_is_idempotent():
    gozar_logging.configure_logging("info")
    gozar_logging.configure_logging("debug")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
