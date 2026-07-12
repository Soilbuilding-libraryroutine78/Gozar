"""Typed domain exceptions and consistent error envelopes.

Gozar exposes two distinct HTTP surfaces with two distinct error formats:

* The **admin control-path** (under ``/api``) uses the steering §18 error
  envelope::

      {"error": {"code": "VALIDATION_ERROR", "message": "...", "details": []}}

* The **proxy data-path** (under ``/v1``) must be a drop-in replacement for the
  OpenAI API, so it emits OpenAI-compatible error JSON so that OpenAI client
  libraries parse it naturally::

      {"error": {"message": "...", "type": "invalid_request_error", "code": "..."}}

Every error condition in the system is expressed as a :class:`GozarError`
subclass. Each subclass carries an HTTP status, a stable machine-readable
``code``, and an OpenAI ``error.type``, so a single exception can be rendered
into either envelope by the appropriate handler. Errors never carry secret
material; messages are safe to return to clients.
"""

from __future__ import annotations

from typing import Any

# OpenAI-compatible ``error.type`` values. These mirror the strings the official
# OpenAI API returns so that client libraries branch on them correctly.
OPENAI_TYPE_INVALID_REQUEST = "invalid_request_error"
OPENAI_TYPE_AUTHENTICATION = "authentication_error"
OPENAI_TYPE_PERMISSION = "permission_error"
OPENAI_TYPE_NOT_FOUND = "not_found_error"
OPENAI_TYPE_RATE_LIMIT = "rate_limit_error"
OPENAI_TYPE_API = "api_error"
OPENAI_TYPE_SERVICE_UNAVAILABLE = "service_unavailable_error"


class GozarError(Exception):
    """Base class for all typed domain exceptions.

    Attributes
    ----------
    status_code:
        HTTP status code this error maps to.
    code:
        Stable, machine-readable error code used in the admin envelope and as the
        OpenAI ``error.code`` field (for example ``VALIDATION_ERROR``).
    message:
        Human-readable, secret-free description safe to return to clients.
    details:
        Optional list of structured detail entries (for example per-field
        validation problems) included in the admin envelope.
    openai_type:
        The OpenAI-compatible ``error.type`` used on the ``/v1`` surface.
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    openai_type: str = OPENAI_TYPE_API

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[Any] | None = None,
        openai_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        if openai_type is not None:
            self.openai_type = openai_type
        self.details: list[Any] = details or []

    def admin_envelope(self) -> dict[str, Any]:
        """Render the steering §18 admin error envelope."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }

    def openai_envelope(self) -> dict[str, Any]:
        """Render an OpenAI-compatible error body for the ``/v1`` surface."""
        return {
            "error": {
                "message": self.message,
                "type": self.openai_type,
                "code": self.code,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(status_code={self.status_code}, "
            f"code={self.code!r}, message={self.message!r})"
        )


class ValidationError(GozarError):
    """The request was malformed or failed validation (HTTP 400)."""

    status_code = 400
    code = "VALIDATION_ERROR"
    openai_type = OPENAI_TYPE_INVALID_REQUEST


class AuthError(GozarError):
    """Authentication failed or was missing (HTTP 401).

    Used both for unauthenticated admin requests (Requirement 16.3) and for
    proxy requests presenting a missing or invalid Client_Token (Requirement
    6.2).
    """

    status_code = 401
    code = "AUTHENTICATION_ERROR"
    openai_type = OPENAI_TYPE_AUTHENTICATION


class PermissionError(GozarError):
    """The caller is authenticated but not authorized (HTTP 403)."""

    status_code = 403
    code = "PERMISSION_DENIED"
    openai_type = OPENAI_TYPE_PERMISSION


class NotFound(GozarError):
    """The requested resource does not exist or is hidden (HTTP 404)."""

    status_code = 404
    code = "NOT_FOUND"
    openai_type = OPENAI_TYPE_NOT_FOUND


class RateLimitError(GozarError):
    """A usage limit was reached or the subject is rejected (HTTP 429).

    Raised when a Client_Token has reached its configured Usage_Limit
    (Requirement 9.2) or is otherwise throttled.
    """

    status_code = 429
    code = "RATE_LIMITED"
    openai_type = OPENAI_TYPE_RATE_LIMIT


class NoAvailableAccount(GozarError):
    """No Upstream_Credential was available to serve the request (HTTP 503).

    Maps to Requirement 6.4 ("no available account").
    """

    status_code = 503
    code = "NO_AVAILABLE_ACCOUNT"
    openai_type = OPENAI_TYPE_SERVICE_UNAVAILABLE


class UpstreamError(GozarError):
    """An upstream Provider call failed (HTTP 502).

    Used when every available Upstream_Credential in a Fallback_Chain has been
    exhausted without a successful response (Requirement 10.3, "all fallbacks
    failed").
    """

    status_code = 502
    code = "UPSTREAM_ERROR"
    openai_type = OPENAI_TYPE_API


class ConfigError(GozarError):
    """The deployment is misconfigured (HTTP 500).

    Raised, for example, when required secret-encryption material is absent or
    invalid at startup (fail closed).
    """

    status_code = 500
    code = "CONFIG_ERROR"
    openai_type = OPENAI_TYPE_API


def register_exception_handlers(app: Any) -> None:
    """Register FastAPI exception handlers for :class:`GozarError`.

    The handler chooses the envelope by request path: requests under ``/v1`` get
    OpenAI-compatible error JSON; everything else (the admin control-path) gets
    the steering §18 admin envelope. App registration is optional and wired in
    later tasks when the routers exist; this helper keeps the rendering decision
    in one place.
    """
    # Imported lazily so this module has no hard dependency on FastAPI being
    # importable (keeps unit tests of the envelopes dependency-light).
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(GozarError)
    async def _handle_gozar_error(request: "Request", exc: GozarError) -> "JSONResponse":
        if request.url.path.startswith("/v1"):
            body = exc.openai_envelope()
        else:
            body = exc.admin_envelope()
        headers: dict[str, str] = {}
        trace_id = getattr(request.state, "gozar_trace_id", None)
        if isinstance(trace_id, str) and trace_id:
            headers = {
                "x-request-id": trace_id,
                "x-gozar-trace-id": trace_id,
            }
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=headers,
        )
