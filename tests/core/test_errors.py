"""Unit tests for typed domain exceptions and error envelopes."""

from __future__ import annotations

from gozar.core import errors


def test_base_error_defaults_to_internal_error():
    err = errors.GozarError("boom")
    assert err.status_code == 500
    assert err.code == "INTERNAL_ERROR"
    assert err.message == "boom"
    assert err.details == []


def test_admin_envelope_shape():
    err = errors.ValidationError("bad field", details=[{"field": "model"}])
    envelope = err.admin_envelope()
    assert envelope == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "bad field",
            "details": [{"field": "model"}],
        }
    }


def test_openai_envelope_shape():
    err = errors.AuthError("missing client token")
    envelope = err.openai_envelope()
    assert envelope == {
        "error": {
            "message": "missing client token",
            "type": errors.OPENAI_TYPE_AUTHENTICATION,
            "code": "AUTHENTICATION_ERROR",
        }
    }


def test_subclass_status_codes_and_types():
    cases = [
        (errors.ValidationError("x"), 400, errors.OPENAI_TYPE_INVALID_REQUEST),
        (errors.AuthError("x"), 401, errors.OPENAI_TYPE_AUTHENTICATION),
        (errors.PermissionError("x"), 403, errors.OPENAI_TYPE_PERMISSION),
        (errors.NotFound("x"), 404, errors.OPENAI_TYPE_NOT_FOUND),
        (errors.RateLimitError("x"), 429, errors.OPENAI_TYPE_RATE_LIMIT),
        (errors.UpstreamError("x"), 502, errors.OPENAI_TYPE_API),
        (errors.NoAvailableAccount("x"), 503, errors.OPENAI_TYPE_SERVICE_UNAVAILABLE),
        (errors.ConfigError("x"), 500, errors.OPENAI_TYPE_API),
    ]
    for err, status, openai_type in cases:
        assert err.status_code == status
        assert err.openai_type == openai_type
        # Both envelopes are always renderable for any subclass.
        assert "error" in err.admin_envelope()
        assert "error" in err.openai_envelope()


def test_overrides_apply():
    err = errors.GozarError(
        "custom", code="CUSTOM", status_code=418, openai_type="api_error"
    )
    assert err.code == "CUSTOM"
    assert err.status_code == 418
    assert err.openai_type == "api_error"


def test_errors_are_raisable_exceptions():
    try:
        raise errors.NotFound("nope")
    except errors.GozarError as exc:
        assert isinstance(exc, Exception)
        assert exc.status_code == 404
