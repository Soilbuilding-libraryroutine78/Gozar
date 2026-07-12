"""Account_Manager service: connecting Upstream_Credentials.

This module implements the Account_Manager described in the design. It owns the
behaviour that connects new Upstream_Credentials to Gozar:

* :func:`begin_subscription_connect` / :func:`complete_subscription_connect` --
  the OAuth 2.0 Authorization Code flow **with PKCE** used to connect a
  Subscription_Account (Requirements 1.1, 1.3, 1.4). The PKCE ``code_verifier``
  and the anti-CSRF ``state`` are generated and held **server-side**, keyed by an
  opaque ``pending_id`` in Redis; the console never sees the verifier. The
  authorization code is exchanged for a token bundle, the provider account
  reference is derived from the access-token claims, and the bundle is persisted
  encrypted (Requirements 1.2, 16.2). On any authorization failure a descriptive
  error is raised and **no account is created** (Requirement 1.3).
* :func:`connect_api_key` -- validates a conventional metered API key against the
  Provider with a cheap upstream call (a models list) **before** creating the
  account, stores the key encrypted, and returns a descriptive error without
  creating an account when validation fails (Requirements 2.1, 2.2, 2.3).

Secret material (the subscription token bundle and the API key) is only ever held
in memory long enough to envelope-encrypt it via :mod:`gozar.core.crypto`; this
module never persists or logs plaintext secrets (Requirements 1.2, 2.2, 16.2,
16.4).

Network and Redis access are injectable (the ``store``, ``exchange``, and
``validate`` parameters) so the flows are unit-testable without real I/O, mirroring
the dependency-injection style of :mod:`gozar.providers.client`. The defaults use
the resilient :class:`~gozar.providers.client.UpstreamClient` for the token
exchange and the validation call, and Redis for the pending PKCE state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.models import (
    AccountUsageLimit,
    ApiKeySecret,
    CredentialKind,
    CredentialStatus,
    SubscriptionSecret,
    UpstreamCredential,
)
from gozar.core.config import Settings, get_settings
from gozar.core.crypto import EncryptedRecord, decrypt, encrypt
from gozar.core.errors import (
    ConfigError,
    NoAvailableAccount,
    NotFound,
    UpstreamError,
    ValidationError,
)
from gozar.providers.client import UpstreamClient
from gozar.providers.registry import ProviderEntry, ProviderId, get_provider
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec

# Redis key namespace for the server-side pending OAuth/PKCE state.
_PENDING_KEY_PREFIX = "acct:pending_connect:"
# How long an in-progress subscription connect may stay pending before the stored
# PKCE verifier/state expires. This is an internal security timeout (an Operator
# is expected to complete the authorize redirect within minutes), not a
# deployment-varying value, so it is a named constant rather than a setting.
_PENDING_TTL_SECONDS = 600
# Device-code sign-in codes expire quickly; keep the server-side challenge only
# long enough for the operator to finish the browser verification page.
_DEVICE_PENDING_TTL_SECONDS = 900
_DEVICE_PENDING_STATUSES = {403, 404}
_DEVICE_FLOW = "device_code"


# --- value objects -----------------------------------------------------------
@dataclass(frozen=True)
class AuthorizationChallenge:
    """The result of :func:`begin_subscription_connect`.

    ``authorize_url`` is the provider URL the Operator must open to grant access;
    ``state`` is the anti-CSRF value echoed back on the callback; ``pending_id`` is
    the opaque handle the console passes to :func:`complete_subscription_connect`.
    The PKCE ``code_verifier`` is intentionally **not** exposed here -- it is held
    server-side keyed by ``pending_id``.
    """

    pending_id: str
    authorize_url: str
    state: str


@dataclass(frozen=True)
class DeviceAuthorizationChallenge:
    """The result of starting a subscription device-code login.

    ``pending_id`` is the opaque server-side handle. ``verification_url`` is opened by
    the Operator in any browser, and ``user_code`` is the one-time code they enter
    there. The upstream ``device_auth_id`` stays server-side in Redis and is never
    exposed to the console.
    """

    pending_id: str
    verification_url: str
    user_code: str
    interval_seconds: int


@dataclass(frozen=True)
class DeviceAuthorizationOutcome:
    """Result of polling a device-code connect attempt."""

    pending: bool
    credential: UpstreamCredential | None = None


@dataclass(frozen=True)
class AccountView:
    """A non-secret summary of a connected Upstream_Credential (Requirement 5.4).

    Returned by :func:`list_accounts` for the console accounts view. It carries the
    Provider, lifecycle status, the configured Usage_Limit (or ``None`` when no limit
    is set), and the current consumption measured against that limit. No secret
    material is ever included (Requirements 16.2, 16.4).
    """

    account_id: uuid.UUID
    provider: str
    kind: CredentialKind
    label: str
    status: CredentialStatus
    connected_at: datetime
    limit: UsageLimitSpec | None
    consumption: float


# --- pending PKCE state store -------------------------------------------------
class PendingConnectStore(Protocol):
    """Server-side store for in-progress subscription-connect PKCE state.

    Implementations persist a small JSON-serialisable mapping (provider, PKCE
    verifier, and state) under an opaque ``pending_id`` with a TTL, so the verifier
    never leaves the server and an abandoned flow expires on its own.
    """

    async def put(
        self, pending_id: str, data: dict[str, Any], ttl_seconds: int
    ) -> None: ...

    async def get(self, pending_id: str) -> dict[str, Any] | None: ...

    async def delete(self, pending_id: str) -> None: ...


class RedisPendingConnectStore:
    """Redis-backed :class:`PendingConnectStore` (the production default).

    Values are stored as JSON under ``acct:pending_connect:<pending_id>`` with an
    expiry so abandoned connect attempts self-clean.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _key(pending_id: str) -> str:
        return f"{_PENDING_KEY_PREFIX}{pending_id}"

    async def put(
        self, pending_id: str, data: dict[str, Any], ttl_seconds: int
    ) -> None:
        await self._client.set(
            self._key(pending_id), json.dumps(data), ex=ttl_seconds
        )

    async def get(self, pending_id: str) -> dict[str, Any] | None:
        raw = await self._client.get(self._key(pending_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def delete(self, pending_id: str) -> None:
        await self._client.delete(self._key(pending_id))


def _default_store(settings: Settings) -> RedisPendingConnectStore:
    """Build the default Redis-backed pending store, failing closed without Redis."""
    if not settings.redis_url:
        raise ConfigError(
            "GOZAR_REDIS_URL is not configured; subscription connect requires Redis "
            "to hold the server-side PKCE state."
        )
    # Imported lazily so unit tests that inject a store never import the redis client.
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return RedisPendingConnectStore(client)


# --- PKCE / OAuth helpers -----------------------------------------------------
def _generate_pkce_pair() -> tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` PKCE pair using the S256 method.

    The verifier is high-entropy URL-safe text; the challenge is the base64url
    (unpadded) SHA-256 of the verifier, as required by RFC 7636.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_authorize_url(entry: ProviderEntry, state: str, challenge: str) -> str:
    """Build the provider authorize URL with PKCE + state query parameters.

    Standard authorization-code + PKCE parameters are always included; any
    provider-specific extra parameters declared in the OAuth metadata
    (``authorize_params``) are merged in as well, since some providers (OpenAI
    Codex, Anthropic) require additional query parameters on the authorize redirect.
    """
    oauth = entry.oauth
    assert oauth is not None  # guaranteed by caller (subscription provider)
    # NOTE (do not "fix" this to the console domain): ``redirect_uri`` is the
    # provider's registered loopback (Codex http://localhost:1455/auth/callback,
    # Anthropic http://localhost:53692/callback). These public OAuth client ids only
    # permit that loopback redirect -- OpenAI/Anthropic reject any other redirect_uri,
    # so it CANNOT be pointed at Gozar's own origin. Completion is therefore manual:
    # the Operator opens this URL, consents, and pastes the resulting redirect URL
    # back (see complete_subscription_connect / extract_code_and_state). This is what
    # makes the connect flow work from any origin (localhost or any domain).
    params = {
        "response_type": "code",
        "client_id": oauth.client_id,
        "redirect_uri": oauth.redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if oauth.scopes:
        params["scope"] = " ".join(oauth.scopes)
    # Provider-specific extras (e.g. id_token_add_organizations, originator, code).
    for key, value in oauth.authorize_params:
        params.setdefault(key, value)
    separator = "&" if "?" in oauth.authorize_url else "?"
    return f"{oauth.authorize_url}{separator}{urlencode(params)}"


def _derive_account_ref(*tokens: str | None) -> str | None:
    """Derive a stable provider-side account reference from token claims.

    Subscription tokens are JWTs whose claims embed a stable account identifier
    (Requirement 1.4). Each token in ``tokens`` is tried in order and the first
    recognisable account claim wins: callers pass the ``id_token`` first and the
    ``access_token`` second, matching the upstream client's claim order (the
    ``id_token_add_organizations`` authorize param surfaces the account id in the
    id_token, with the access token as a fallback). The signature is verified by the
    Provider, not by Gozar, so claims are decoded without signature verification
    purely to read the account id. Returns ``None`` when no token is a JWT carrying a
    recognisable account claim, in which case the locally generated credential id
    remains the unique account identifier.
    """
    for token in tokens:
        ref = _account_ref_from_token(token)
        if ref:
            return ref
    return None


def _account_ref_from_token(token: str | None) -> str | None:
    """Read a provider account reference from one JWT's claims (or ``None``)."""
    if not token:
        return None
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    # Provider-namespaced auth claim (e.g. OpenAI embeds account info here).
    namespaced = claims.get("https://api.openai.com/auth")
    if isinstance(namespaced, dict):
        for key in ("chatgpt_account_id", "account_id", "organization_id"):
            value = namespaced.get(key)
            if value:
                return str(value)
    # Generic top-level fallbacks.
    for key in ("account_id", "organization_id", "org_id", "sub"):
        value = claims.get(key)
        if value:
            return str(value)
    return None


def _expires_at_from_token(token: dict[str, Any]) -> datetime | None:
    """Compute the access-token expiry from a token response.

    Prefers an absolute ``expires_at`` (epoch seconds); otherwise derives it from a
    relative ``expires_in`` (seconds from now). Returns ``None`` when neither is
    present.
    """
    now = datetime.now(timezone.utc)
    if token.get("expires_at") is not None:
        try:
            return datetime.fromtimestamp(float(token["expires_at"]), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
    if token.get("expires_in") is not None:
        try:
            return now + timedelta(seconds=float(token["expires_in"]))
        except (TypeError, ValueError, OverflowError):
            return None
    return None


# Injectable I/O hooks (defaults perform real network calls via UpstreamClient).
ExchangeFn = Callable[[ProviderEntry, str, str], Awaitable[dict[str, Any]]]
ValidateFn = Callable[[ProviderEntry, str], Awaitable[None]]
DeviceCodeRequestFn = Callable[[ProviderEntry], Awaitable[dict[str, Any]]]
DeviceCodePollFn = Callable[[ProviderEntry, str, str], Awaitable[dict[str, Any] | None]]


async def _post_token_request(
    entry: ProviderEntry,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    """POST a token request in the provider's expected format and decode the JSON.

    OAuth token endpoints differ in the body encoding they accept: the OpenAI
    Codex endpoint expects a standard RFC 6749 form-encoded body, while Anthropic's
    endpoint expects a JSON body. ``token_request_format`` on the provider's OAuth
    metadata selects which is sent. Raises :class:`UpstreamError` on a non-2xx
    response (surfaced by the resilient :class:`UpstreamClient`).
    """
    oauth = entry.oauth
    assert oauth is not None
    async with UpstreamClient(entry, settings=settings) as client:
        if oauth.token_request_format == "json":
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            response = await client.request(
                "POST", oauth.token_url, json=payload, headers=headers
            )
        else:
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            response = await client.request(
                "POST",
                oauth.token_url,
                content=urlencode(payload).encode(),
                headers=headers,
            )
    return response.json()


async def _default_exchange(
    entry: ProviderEntry,
    code: str,
    verifier: str,
    *,
    state: str | None = None,
    settings: Settings,
) -> dict[str, Any]:
    """Exchange an authorization code for a token bundle at the token endpoint.

    Sends a standard RFC 6749 ``authorization_code`` grant including the PKCE
    ``code_verifier`` to the provider token URL via the resilient
    :class:`UpstreamClient`, encoded in the provider's expected body format (form or
    JSON). When the provider's OAuth metadata sets
    ``include_state_in_token_exchange`` (Anthropic), the anti-CSRF ``state`` is
    echoed back in the exchange body. Raises :class:`UpstreamError` on a non-2xx
    response.
    """
    oauth = entry.oauth
    assert oauth is not None
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oauth.redirect_uri,
        "client_id": oauth.client_id,
        "code_verifier": verifier,
    }
    if oauth.include_state_in_token_exchange and state:
        payload["state"] = state
    return await _post_token_request(entry, payload, settings=settings)


def _oauth_issuer_base(entry: ProviderEntry) -> str:
    """Return the OAuth issuer origin for device-auth endpoints."""
    oauth = entry.oauth
    assert oauth is not None
    split = urlsplit(oauth.token_url)
    return f"{split.scheme}://{split.netloc}".rstrip("/")


async def _default_request_device_code(
    entry: ProviderEntry,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Request a Codex device-code challenge from the OpenAI auth service."""
    oauth = entry.oauth
    assert oauth is not None
    url = f"{_oauth_issuer_base(entry)}/api/accounts/deviceauth/usercode"
    async with httpx.AsyncClient(timeout=settings.upstream_request_timeout_seconds) as client:
        response = await client.post(url, json={"client_id": oauth.client_id})
    if response.status_code >= 400:
        raise UpstreamError(
            f"device-code authorization was rejected by provider "
            f"{entry.provider_id.value!r}",
            details=[{"upstream_status": response.status_code}],
        )
    return response.json()


async def _default_poll_device_code(
    entry: ProviderEntry,
    device_auth_id: str,
    user_code: str,
    *,
    settings: Settings,
) -> dict[str, Any] | None:
    """Poll the OpenAI device-code endpoint once.

    ``None`` means the user has not approved the code yet. Success returns the
    authorization-code payload that can be exchanged at the OAuth token endpoint.
    """
    url = f"{_oauth_issuer_base(entry)}/api/accounts/deviceauth/token"
    body = {"device_auth_id": device_auth_id, "user_code": user_code}
    async with httpx.AsyncClient(timeout=settings.upstream_request_timeout_seconds) as client:
        response = await client.post(url, json=body)
    if response.status_code in _DEVICE_PENDING_STATUSES:
        return None
    if response.status_code >= 400:
        raise UpstreamError(
            f"device-code authorization failed for provider "
            f"{entry.provider_id.value!r}",
            details=[{"upstream_status": response.status_code}],
        )
    return response.json()


async def _default_device_exchange(
    entry: ProviderEntry,
    authorization_code: str,
    code_verifier: str,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Exchange an approved device authorization code for subscription tokens."""
    oauth = entry.oauth
    assert oauth is not None
    payload = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": f"{_oauth_issuer_base(entry)}/deviceauth/callback",
        "client_id": oauth.client_id,
        "code_verifier": code_verifier,
    }
    return await _post_token_request(entry, payload, settings=settings)


async def _default_validate_api_key(
    entry: ProviderEntry,
    api_key: str,
    *,
    settings: Settings,
) -> None:
    """Validate an API key with a cheap upstream call (a models list).

    Raises :class:`UpstreamError` when the Provider rejects the key, which the
    caller translates into a descriptive :class:`ValidationError`.
    """
    if entry.model_listing_path is None:
        raise ValidationError(
            f"provider {entry.provider_id.value!r} does not expose an API-key validation endpoint"
        )
    headers = {"Authorization": f"Bearer {api_key}"}
    async with UpstreamClient(entry, settings=settings) as client:
        await client.request("GET", entry.model_listing_path, headers=headers)


# --- persistence helpers ------------------------------------------------------
def _encrypt_bundle(bundle: dict[str, Any], settings: Settings) -> EncryptedRecord:
    """Envelope-encrypt a JSON-serialisable secret bundle."""
    plaintext = json.dumps(bundle, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return encrypt(plaintext, settings=settings)


async def _persist_subscription_credential(
    session: AsyncSession,
    entry: ProviderEntry,
    token: dict[str, Any],
    *,
    label: str | None,
    settings: Settings,
) -> UpstreamCredential:
    """Persist a successful subscription token bundle as an encrypted credential."""
    access_token = token.get("access_token")
    if not access_token:
        raise ValidationError(
            f"subscription authorization for provider {entry.provider_id.value!r} "
            f"returned no access token"
        )

    account_ref = _derive_account_ref(token.get("id_token"), str(access_token))
    expires_at = _expires_at_from_token(token)
    bundle = {
        "access_token": access_token,
        "refresh_token": token.get("refresh_token"),
        "account_id": account_ref,
        "scopes": token.get("scope") or token.get("scopes"),
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
    }
    record = _encrypt_bundle(bundle, settings)

    credential = UpstreamCredential(
        id=uuid.uuid4(),
        provider=entry.provider_id.value,
        kind=CredentialKind.SUBSCRIPTION,
        label=label or entry.provider_id.value,
        status=CredentialStatus.ACTIVE,
        provider_account_ref=account_ref,
    )
    session.add(credential)
    await session.flush()

    session.add(
        SubscriptionSecret(
            account_id=credential.id,
            ciphertext=record.ciphertext,
            nonce=record.nonce,
            wrapped_dek=record.wrapped_dek,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return credential


# --- public service surface ---------------------------------------------------
async def begin_subscription_connect(
    provider: str | ProviderId,
    *,
    settings: Settings | None = None,
    store: PendingConnectStore | None = None,
) -> AuthorizationChallenge:
    """Start connecting a Subscription_Account via OAuth + PKCE (Requirement 1.1).

    Generates a PKCE verifier/challenge and an anti-CSRF ``state``, stores the
    verifier and state server-side under an opaque ``pending_id`` (in Redis by
    default), and returns the provider authorize URL the Operator must visit. The
    verifier is never returned to the caller.

    Raises :class:`ValidationError` if ``provider`` is unknown or does not use
    subscription OAuth, and :class:`ConfigError` (fail closed) if the provider's
    OAuth metadata or the pending-state store is not configured.
    """
    settings = settings or get_settings()
    entry = get_provider(provider, settings=settings)
    if not entry.is_subscription or entry.oauth is None:
        raise ValidationError(
            f"provider {entry.provider_id.value!r} does not use subscription OAuth; "
            f"use connect_api_key instead"
        )

    store = store or _default_store(settings)
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    pending_id = secrets.token_urlsafe(32)

    await store.put(
        pending_id,
        {"provider": entry.provider_id.value, "verifier": verifier, "state": state},
        _PENDING_TTL_SECONDS,
    )

    return AuthorizationChallenge(
        pending_id=pending_id,
        authorize_url=_build_authorize_url(entry, state, challenge),
        state=state,
    )


async def begin_device_subscription_connect(
    provider: str | ProviderId,
    *,
    settings: Settings | None = None,
    store: PendingConnectStore | None = None,
    request_device_code: DeviceCodeRequestFn | None = None,
) -> DeviceAuthorizationChallenge:
    """Start a subscription connect using OpenAI Codex device-code authorization.

    This avoids the fragile ``localhost:1455`` browser redirect entirely. It is
    available for Codex because the OpenAI auth service exposes Codex device-auth
    endpoints; other subscription providers should continue to use their normal
    connect flow unless they publish a compatible device-code endpoint.
    """
    settings = settings or get_settings()
    entry = get_provider(provider, settings=settings)
    if entry.provider_id is not ProviderId.CODEX:
        raise ValidationError(
            "device-code subscription connect is currently supported only for the "
            "Codex provider"
        )
    if not entry.is_subscription or entry.oauth is None:
        raise ValidationError(
            f"provider {entry.provider_id.value!r} does not use subscription OAuth"
        )

    store = store or _default_store(settings)
    request_fn: DeviceCodeRequestFn = request_device_code or (
        lambda e: _default_request_device_code(e, settings=settings)
    )
    try:
        response = await request_fn(entry)
    except UpstreamError as exc:
        raise ValidationError(
            "device-code authorization is unavailable for this provider or workspace; "
            "use the browser redirect flow or reconnect with an API key"
        ) from exc

    device_auth_id = str(response.get("device_auth_id") or "")
    user_code = str(response.get("user_code") or response.get("usercode") or "")
    if not device_auth_id or not user_code:
        raise ValidationError(
            f"device-code authorization for provider {entry.provider_id.value!r} "
            f"returned an incomplete challenge"
        )
    try:
        interval = int(str(response.get("interval") or "5"))
    except (TypeError, ValueError):
        interval = 5
    interval = max(2, min(interval, 30))

    pending_id = secrets.token_urlsafe(32)
    await store.put(
        pending_id,
        {
            "provider": entry.provider_id.value,
            "flow": _DEVICE_FLOW,
            "device_auth_id": device_auth_id,
            "user_code": user_code,
        },
        _DEVICE_PENDING_TTL_SECONDS,
    )

    return DeviceAuthorizationChallenge(
        pending_id=pending_id,
        verification_url=f"{_oauth_issuer_base(entry)}/codex/device",
        user_code=user_code,
        interval_seconds=interval,
    )


def extract_code_and_state(pasted: str) -> tuple[str, str | None]:
    """Extract the authorization ``code`` (and ``state`` if present) from operator input.

    A **pure** helper (no I/O, no state) for the domain-independent manual-paste
    completion path. The public OAuth clients these subscription providers use only
    permit the provider's own **loopback** redirect (Codex
    ``http://localhost:1455/auth/callback``, Anthropic
    ``http://localhost:53692/callback``); the provider rejects any other
    ``redirect_uri`` for these client ids, so Gozar -- a web app served from an
    arbitrary origin -- can never receive that callback itself. Instead the Operator
    completes the browser consent in *their own* browser, the provider redirects to
    the loopback URL (which does not load), and the Operator copies that full address
    back into the console. This is what makes the flow work from any origin.

    The Operator may paste either:

    * the **full redirect URL** (e.g.
      ``http://localhost:1455/auth/callback?code=AUTH&state=XYZ``), or
    * just the bare authorization ``code``.

    When ``pasted`` carries a ``code`` query parameter it is parsed with a real URL
    parser (:mod:`urllib.parse`, never a regex) and ``(code, state_or_None)`` is
    returned. Otherwise the stripped input is treated as a bare code and ``state`` is
    ``None``.
    """
    raw = (pasted or "").strip()
    if "code=" not in raw:
        # No query parameters at all: treat the whole input as a bare code.
        return raw, None
    split = urlsplit(raw)
    # Accept both a full URL (with a query) and a bare "code=...&state=..." fragment.
    query = split.query if split.query else raw
    params = parse_qs(query)
    codes = params.get("code")
    if not codes:
        # "code=" appeared but not as a parseable query parameter; treat as bare code.
        return raw, None
    states = params.get("state")
    state = states[0] if states else None
    return codes[0], state


async def complete_subscription_connect(
    session: AsyncSession,
    pending_id: str,
    code: str,
    state: str | None = None,
    *,
    label: str | None = None,
    settings: Settings | None = None,
    store: PendingConnectStore | None = None,
    exchange: ExchangeFn | None = None,
) -> UpstreamCredential:
    """Complete a subscription connect: validate, exchange, and persist.

    ``code`` may be either a bare authorization code **or** the full redirect URL the
    Operator pasted from their browser (see :func:`extract_code_and_state`); the
    authorization code -- and, when present, the ``state`` -- are extracted from it.
    ``state`` is optional: an explicitly supplied value takes precedence, otherwise a
    ``state`` parsed out of the pasted redirect URL is used.

    Looks up the server-side pending state by ``pending_id``, then:

    * **If a state is available** (passed by the caller or extracted from the pasted
      URL), it is verified in constant time against the stored pending state
      (anti-CSRF), as before.
    * **If no state is available** (the Operator pasted only the bare ``code``), the
      flow proceeds keyed solely by the opaque, server-generated ``pending_id``. That
      handle is itself high-entropy and unguessable, so it authenticates the
      completion on its own. This is the documented manual-paste fallback that keeps
      the flow domain-independent.

    The stored PKCE verifier is used for the token exchange, the provider account
    reference is derived from the token claims, and the encrypted bundle is persisted
    as a new Subscription_Account (Requirements 1.2, 1.4, 16.2).

    On **any** failure -- unknown/expired ``pending_id``, ``state`` mismatch, or a
    failed token exchange -- a descriptive :class:`ValidationError` is raised and no
    account is created (Requirement 1.3). The pending state is consumed on success
    and on a definitive validation failure so it cannot be replayed.
    """
    settings = settings or get_settings()
    store = store or _default_store(settings)

    # Normalise a manually pasted authorization (bare code or full redirect URL).
    code, parsed_state = extract_code_and_state(code)
    # An explicitly supplied state wins; otherwise fall back to any state parsed from
    # the pasted redirect URL. ``None``/empty means "no state available".
    supplied_state = state if state else None
    effective_state = supplied_state or parsed_state

    pending = await store.get(pending_id)
    if pending is None:
        raise ValidationError(
            "subscription connect session is unknown or has expired; restart the "
            "connection"
        )

    expected_state = str(pending.get("state", ""))
    if effective_state:
        # A state is available: validate it in constant time. A mismatch is a CSRF
        # signal -- consume the pending state and refuse.
        if not expected_state or not hmac.compare_digest(
            expected_state, effective_state
        ):
            await store.delete(pending_id)
            raise ValidationError(
                "subscription connect state did not match; the request may be forged "
                "or stale"
            )
    # else: manual-paste fallback -- no state to check, proceed keyed solely by the
    # unguessable pending_id (which already authenticates this completion).

    provider_value = str(pending.get("provider", ""))
    verifier = str(pending.get("verifier", ""))
    entry = get_provider(provider_value, settings=settings)

    # The token exchange echoes back the canonical stored state when the provider
    # requires it (Anthropic); this is the exact value used in the authorize request,
    # so it is correct regardless of which paste path the Operator used.
    exchange_fn: ExchangeFn = exchange or (
        lambda e, c, v: _default_exchange(
            e, c, v, state=expected_state or None, settings=settings
        )
    )
    try:
        token = await exchange_fn(entry, code, verifier)
    except UpstreamError as exc:
        # Exchange failed at the provider: descriptive error, no account created.
        raise ValidationError(
            f"subscription authorization failed for provider "
            f"{entry.provider_id.value!r}: token exchange was rejected"
        ) from exc

    credential = await _persist_subscription_credential(
        session,
        entry,
        token,
        label=label,
        settings=settings,
    )
    # Authorization succeeded; safe to consume the pending state.
    await store.delete(pending_id)
    return credential


async def complete_device_subscription_connect(
    session: AsyncSession,
    pending_id: str,
    *,
    label: str | None = None,
    settings: Settings | None = None,
    store: PendingConnectStore | None = None,
    poll_device_code: DeviceCodePollFn | None = None,
    exchange: ExchangeFn | None = None,
) -> DeviceAuthorizationOutcome:
    """Poll and complete a Codex device-code subscription connect.

    Returns ``pending=True`` while the Operator still needs to approve the one-time
    code in their browser. Once OpenAI issues an authorization code, it is exchanged
    for the encrypted subscription credential and the pending challenge is consumed.
    """
    settings = settings or get_settings()
    store = store or _default_store(settings)
    pending = await store.get(pending_id)
    if pending is None:
        raise ValidationError(
            "device-code connect session is unknown or has expired; restart the "
            "connection"
        )
    if pending.get("flow") != _DEVICE_FLOW:
        raise ValidationError("connect session is not a device-code authorization")

    provider_value = str(pending.get("provider", ""))
    device_auth_id = str(pending.get("device_auth_id", ""))
    user_code = str(pending.get("user_code", ""))
    if not provider_value or not device_auth_id or not user_code:
        await store.delete(pending_id)
        raise ValidationError(
            "device-code connect session is incomplete; restart the connection"
        )

    entry = get_provider(provider_value, settings=settings)
    poll_fn: DeviceCodePollFn = poll_device_code or (
        lambda e, auth_id, code: _default_poll_device_code(
            e, auth_id, code, settings=settings
        )
    )
    try:
        device_token = await poll_fn(entry, device_auth_id, user_code)
    except UpstreamError as exc:
        await store.delete(pending_id)
        raise ValidationError(
            "device-code authorization was rejected or expired; restart the connection"
        ) from exc
    if device_token is None:
        return DeviceAuthorizationOutcome(pending=True)

    authorization_code = str(device_token.get("authorization_code") or "")
    code_verifier = str(device_token.get("code_verifier") or "")
    if not authorization_code or not code_verifier:
        await store.delete(pending_id)
        raise ValidationError(
            "device-code authorization returned an incomplete token exchange payload"
        )

    exchange_fn: ExchangeFn = exchange or (
        lambda e, c, v: _default_device_exchange(e, c, v, settings=settings)
    )
    try:
        token = await exchange_fn(entry, authorization_code, code_verifier)
    except UpstreamError as exc:
        await store.delete(pending_id)
        raise ValidationError(
            f"device-code authorization failed for provider "
            f"{entry.provider_id.value!r}: token exchange was rejected"
        ) from exc

    credential = await _persist_subscription_credential(
        session,
        entry,
        token,
        label=label,
        settings=settings,
    )
    await store.delete(pending_id)
    return DeviceAuthorizationOutcome(pending=False, credential=credential)


async def connect_api_key(
    session: AsyncSession,
    provider: str | ProviderId,
    api_key: str,
    *,
    label: str | None = None,
    settings: Settings | None = None,
    validate: ValidateFn | None = None,
) -> UpstreamCredential:
    """Connect an API_Key_Account after validating the key (Requirements 2.1-2.3).

    Validates ``api_key`` against the Provider with a cheap upstream call (a models
    list) **before** creating anything. On success the key is stored envelope-
    encrypted and a new API_Key_Account is returned. If validation fails, a
    descriptive :class:`ValidationError` is raised and **no account is created**
    (Requirement 2.3).

    Raises :class:`ValidationError` if ``provider`` is unknown, uses subscription
    OAuth, or ``api_key`` is empty.
    """
    settings = settings or get_settings()
    if not api_key or not api_key.strip():
        raise ValidationError("an API key is required")

    entry = get_provider(provider, settings=settings)
    if entry.is_subscription:
        raise ValidationError(
            f"provider {entry.provider_id.value!r} uses subscription OAuth; use "
            f"begin_subscription_connect instead"
        )

    validate_fn: ValidateFn = validate or (
        lambda e, k: _default_validate_api_key(e, k, settings=settings)
    )
    try:
        await validate_fn(entry, api_key)
    except UpstreamError as exc:
        # Provider rejected the key: descriptive error, no account created.
        raise ValidationError(
            f"API key validation failed for provider {entry.provider_id.value!r}; "
            f"the key was not accepted"
        ) from exc

    record = _encrypt_bundle({"api_key": api_key}, settings)
    credential = UpstreamCredential(
        id=uuid.uuid4(),
        provider=entry.provider_id.value,
        kind=CredentialKind.API_KEY,
        label=label or entry.provider_id.value,
        status=CredentialStatus.ACTIVE,
        provider_account_ref=None,
    )
    session.add(credential)
    await session.flush()

    session.add(
        ApiKeySecret(
            account_id=credential.id,
            ciphertext=record.ciphertext,
            nonce=record.nonce,
            wrapped_dek=record.wrapped_dek,
        )
    )
    await session.flush()
    return credential


# --- account lifecycle, limits, and the refresh-window predicate --------------
# Injectable consumption lookup. Given an account id and its current limit spec, it
# returns the consumption already aggregated over the limit's active window. The
# durable usage counters are owned by the Usage_Recorder; the real lookup (reading
# those Redis counters) is now wired at the router layer, while this default reports
# zero so the service stays unit-testable without Redis.
ConsumptionLookup = Callable[[uuid.UUID, "UsageLimitSpec | None"], Awaitable[float]]


async def _zero_consumption(
    account_id: uuid.UUID, limit: UsageLimitSpec | None
) -> float:
    """Default :data:`ConsumptionLookup`: report zero in a no-Redis context."""
    return 0.0


def refresh_needed(
    expires_at: datetime,
    now: datetime,
    renewal_window: timedelta,
) -> bool:
    """Return whether a Subscription_Token must be refreshed (Requirement 3.1).

    This is a **pure** predicate (no clock, no I/O, no state): refresh is needed
    exactly when the time remaining until ``expires_at`` is at or below
    ``renewal_window``. Already-expired tokens have zero or negative remaining time,
    which is within any non-negative window, so they always need refresh.

    Args:
        expires_at: The access-token expiry timestamp.
        now: The reference "current" time to measure against.
        renewal_window: How long before expiry a refresh becomes due.

    Returns:
        ``True`` if ``(expires_at - now) <= renewal_window``, else ``False``.
    """
    return (expires_at - now) <= renewal_window


async def _load_live_credential(
    session: AsyncSession, account_id: uuid.UUID
) -> UpstreamCredential:
    """Load a non-deleted credential by id or raise :class:`NotFound`.

    A credential whose ``deleted_at`` is set is treated as absent (hidden with 404
    per the fail-closed convention), so lifecycle operations cannot resurrect a
    deleted account.
    """
    credential = await session.get(UpstreamCredential, account_id)
    if credential is None or credential.deleted_at is not None:
        raise NotFound(f"upstream credential {account_id} was not found")
    return credential


def _row_to_spec(row: AccountUsageLimit) -> UsageLimitSpec:
    """Map a persisted :class:`AccountUsageLimit` row to a :class:`UsageLimitSpec`.

    ``limit_value`` and ``capacity`` are stored as SQL ``Numeric`` (Decimal) and are
    coerced to ``float`` to match the value-object shape.
    """
    return UsageLimitSpec(
        metric=row.metric,
        limit_value=float(row.limit_value),
        capacity=None if row.capacity is None else float(row.capacity),
        window=row.window,
    )


async def set_usage_limit(
    session: AsyncSession,
    account_id: uuid.UUID,
    limit: UsageLimitSpec,
) -> None:
    """Persist (create or replace) an account's Usage_Limit (Requirements 4.1, 4.4).

    The limit is stored as a single :class:`AccountUsageLimit` row per account and is
    applied to subsequent routing decisions without requiring reconnection of the
    account (Requirement 4.4) -- callers always evaluate against the most recently
    persisted spec. Raises :class:`NotFound` if the account does not exist or has
    been deleted.
    """
    await _load_live_credential(session, account_id)

    existing = (
        await session.execute(
            select(AccountUsageLimit).where(
                AccountUsageLimit.subject_id == account_id
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            AccountUsageLimit(
                id=uuid.uuid4(),
                subject_kind="account",
                subject_id=account_id,
                metric=limit.metric,
                limit_value=limit.limit_value,
                capacity=limit.capacity,
                window=limit.window,
            )
        )
    else:
        existing.metric = limit.metric
        existing.limit_value = limit.limit_value
        existing.capacity = limit.capacity
        existing.window = limit.window
    await session.flush()


async def set_enabled(
    session: AsyncSession,
    account_id: uuid.UUID,
    enabled: bool,
) -> None:
    """Enable or disable an Upstream_Credential (Requirements 5.1, 5.2).

    Disabling moves the credential to ``DISABLED`` so the Flow_Controller treats it
    as unavailable (Requirement 5.1). Enabling moves a ``DISABLED`` credential back
    to ``ACTIVE`` so it becomes available again, subject to its Usage_Limit and
    authorization state (Requirement 5.2). A credential that requires reauthorization
    is left untouched when enabling, because enabling cannot repair its auth state --
    it must be reconnected. Raises :class:`NotFound` if the account does not exist or
    has been deleted.
    """
    credential = await _load_live_credential(session, account_id)
    if enabled:
        if credential.status is CredentialStatus.DISABLED:
            credential.status = CredentialStatus.ACTIVE
    else:
        credential.status = CredentialStatus.DISABLED
    await session.flush()


async def delete(session: AsyncSession, account_id: uuid.UUID) -> None:
    """Delete an Upstream_Credential, removing secrets but keeping history (Req 5.3).

    Hard-deletes the encrypted secret material (the subscription bundle and/or API
    key rows) so no credential material remains, and soft-deletes the credential row
    by stamping ``deleted_at``. The credential row and all previously recorded usage
    history are retained for reporting, and the Flow_Controller skips the row because
    ``deleted_at`` is set (Requirement 11.2). Raises :class:`NotFound` if the account
    does not exist or has already been deleted.
    """
    credential = await _load_live_credential(session, account_id)

    # Hard-delete the secret material rows (both kinds; only one exists in practice).
    await session.execute(
        sa_delete(SubscriptionSecret).where(
            SubscriptionSecret.account_id == account_id
        )
    )
    await session.execute(
        sa_delete(ApiKeySecret).where(ApiKeySecret.account_id == account_id)
    )

    # Soft-delete the credential row; usage history is left intact for reporting.
    credential.deleted_at = datetime.now(timezone.utc)
    await session.flush()


async def list_accounts(
    session: AsyncSession,
    *,
    consumption_lookup: ConsumptionLookup | None = None,
) -> list[AccountView]:
    """Return non-secret summaries of all connected accounts (Requirement 5.4).

    For each live (non-deleted) Upstream_Credential the view carries its Provider,
    status, configured Usage_Limit, and current consumption. Consumption is read via
    ``consumption_lookup`` (wired at the router layer to the Usage_Recorder counters;
    defaulting to zero in a no-Redis context) so the accounts view never depends on
    secret material. Deleted accounts are excluded.
    """
    lookup = consumption_lookup or _zero_consumption

    credentials = list(
        (
            await session.execute(
                select(UpstreamCredential)
                .where(UpstreamCredential.deleted_at.is_(None))
                .order_by(UpstreamCredential.connected_at)
            )
        )
        .scalars()
        .all()
    )

    limit_rows = list(
        (await session.execute(select(AccountUsageLimit))).scalars().all()
    )
    limits_by_account = {row.subject_id: _row_to_spec(row) for row in limit_rows}

    views: list[AccountView] = []
    for credential in credentials:
        limit = limits_by_account.get(credential.id)
        consumption = await lookup(credential.id, limit)
        views.append(
            AccountView(
                account_id=credential.id,
                provider=credential.provider,
                kind=credential.kind,
                label=credential.label,
                status=credential.status,
                connected_at=credential.connected_at,
                limit=limit,
                consumption=consumption,
            )
        )
    return views


# --- locked subscription token refresh and lazy usable-token access -----------
# Redis key namespace and timeouts for the per-account refresh lock. The lock
# serialises refreshes of a single Subscription_Account so concurrent refreshes
# cannot clobber a newly rotated refresh token (the "token sink" problem from the
# design's research). These are internal safety timeouts, not deployment-varying
# values, so they are named constants rather than settings.
_REFRESH_LOCK_KEY_PREFIX = "acct:refresh_lock:"
# Maximum time a held refresh lock survives before Redis auto-releases it, so a
# crashed holder cannot wedge an account forever. Comfortably covers a token
# exchange (which is itself bounded by the upstream request timeout + retries).
_REFRESH_LOCK_TIMEOUT_SECONDS = 30
# How long to wait to acquire the lock before giving up (a concurrent refresh is
# in progress); kept short so the hot path never blocks for long.
_REFRESH_LOCK_BLOCKING_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class RefreshOutcome:
    """The result of :func:`refresh_subscription`.

    ``refreshed`` is ``True`` when a token exchange succeeded and the encrypted
    bundle was replaced (Requirement 3.2). ``requires_reauth`` is ``True`` when the
    refresh failed and the Subscription_Account was marked as requiring
    reauthorization, with ``reason`` carrying the recorded failure reason
    (Requirement 3.3). ``expires_at`` is the new access-token expiry on success.
    """

    account_id: uuid.UUID
    refreshed: bool
    requires_reauth: bool
    reason: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class ProviderCredentialMaterial:
    """Decrypted credential material returned by :func:`get_usable_token`.

    Carries exactly what the data path needs to authenticate an upstream call: for
    a Subscription_Account the ``access_token`` (and the ``provider_account_ref``
    used as an account-id header), and for an API_Key_Account the ``api_key``. No
    refresh token or other long-lived secret beyond what is needed to make the call
    is exposed, and this object is never logged (Requirements 16.2, 16.4).
    """

    account_id: uuid.UUID
    provider: str
    kind: CredentialKind
    access_token: str | None
    api_key: str | None
    provider_account_ref: str | None
    expires_at: datetime | None


# Injectable per-account lock factory. Given an account id it returns an async
# context manager that, on enter, holds an exclusive lock for that account. The
# default builds a Redis lock; tests inject a fake to avoid real Redis.
LockFactory = Callable[[uuid.UUID], AbstractAsyncContextManager[Any]]

# Injectable refresh exchange. Given the provider entry and the stored refresh
# token it returns the provider's token response. Mirrors :data:`ExchangeFn` for
# the connect flow so the refresh is unit-testable without real network I/O.
RefreshFn = Callable[[ProviderEntry, str], Awaitable[dict[str, Any]]]


def _default_lock_factory(settings: Settings) -> LockFactory:
    """Build the default Redis-backed per-account lock factory (fail closed)."""
    if not settings.redis_url:
        raise ConfigError(
            "GOZAR_REDIS_URL is not configured; subscription token refresh requires "
            "Redis to hold the per-account refresh lock."
        )
    # Imported lazily so unit tests that inject a lock never import the redis client.
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.redis_url, decode_responses=True)

    def factory(account_id: uuid.UUID) -> AbstractAsyncContextManager[Any]:
        return client.lock(
            f"{_REFRESH_LOCK_KEY_PREFIX}{account_id}",
            timeout=_REFRESH_LOCK_TIMEOUT_SECONDS,
            blocking_timeout=_REFRESH_LOCK_BLOCKING_TIMEOUT_SECONDS,
        )

    return factory


async def _default_refresh(
    entry: ProviderEntry,
    refresh_token: str,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Exchange a refresh token for a new token bundle at the token endpoint.

    Sends a standard RFC 6749 ``refresh_token`` grant to the provider token URL via
    the resilient :class:`UpstreamClient`, encoded in the provider's expected body
    format (form or JSON). Raises :class:`UpstreamError` on a non-2xx response, which
    the caller turns into a ``requires_reauth`` marking.
    """
    oauth = entry.oauth
    assert oauth is not None
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": oauth.client_id,
    }
    return await _post_token_request(entry, payload, settings=settings)


def _decrypt_bundle(secret: SubscriptionSecret, settings: Settings) -> dict[str, Any]:
    """Decrypt and JSON-decode an envelope-encrypted subscription bundle."""
    plaintext = decrypt(
        secret.ciphertext,
        secret.nonce,
        secret.wrapped_dek,
        settings=settings,
    )
    return json.loads(plaintext.decode("utf-8"))


async def _mark_requires_reauth(
    session: AsyncSession,
    credential: UpstreamCredential,
    reason: str,
) -> RefreshOutcome:
    """Mark a Subscription_Account as requiring reauthorization (Requirement 3.3).

    Sets the credential status to ``REQUIRES_REAUTH`` and records ``reason`` so the
    Flow_Controller treats it as unavailable (Requirement 3.4) and the Operator can
    see why it must be reconnected.
    """
    credential.status = CredentialStatus.REQUIRES_REAUTH
    credential.requires_reauth_reason = reason
    await session.flush()
    return RefreshOutcome(
        account_id=credential.id,
        refreshed=False,
        requires_reauth=True,
        reason=reason,
        expires_at=None,
    )


async def refresh_subscription(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    refresh: RefreshFn | None = None,
    lock_factory: LockFactory | None = None,
) -> RefreshOutcome:
    """Refresh a Subscription_Token under a per-account lock (Requirements 3.2, 3.3).

    Acquires an exclusive per-account Redis lock, then re-reads and decrypts the
    stored bundle **inside** the lock so a concurrent refresh that already rotated
    the refresh token is observed rather than clobbered. The stored ``refresh_token``
    is exchanged at the provider token endpoint (via the injectable ``refresh`` hook,
    defaulting to the resilient :class:`UpstreamClient`).

    On success the new bundle is re-encrypted and replaces the stored
    :class:`SubscriptionSecret` (ciphertext + expiry), the provider may rotate the
    refresh token (the new one is stored, otherwise the prior one is kept), and any
    prior reauthorization flag is cleared (Requirement 3.2). On any failure -- no
    stored refresh token, a rejected exchange, or a response without an access token
    -- the account is marked ``REQUIRES_REAUTH`` with a recorded reason and no secret
    is mutated (Requirement 3.3).

    Raises :class:`NotFound` if the account does not exist or has been deleted, and
    :class:`ValidationError` if the account is not a Subscription_Account.
    """
    settings = settings or get_settings()
    credential = await _load_live_credential(session, account_id)
    if credential.kind is not CredentialKind.SUBSCRIPTION:
        raise ValidationError(
            f"account {account_id} is not a subscription account; only subscription "
            f"tokens are refreshed"
        )

    entry = get_provider(credential.provider, settings=settings)
    lock_factory = lock_factory or _default_lock_factory(settings)
    refresh_fn: RefreshFn = refresh or (
        lambda e, rt: _default_refresh(e, rt, settings=settings)
    )

    async with lock_factory(account_id):
        # Re-load the secret inside the lock: a concurrent refresh may have already
        # rotated the bundle while we waited to acquire the lock.
        secret = await session.get(SubscriptionSecret, account_id)
        if secret is None:
            raise NotFound(
                f"subscription secret for account {account_id} was not found"
            )

        bundle = _decrypt_bundle(secret, settings)
        refresh_token = bundle.get("refresh_token")
        if not refresh_token:
            return await _mark_requires_reauth(
                session,
                credential,
                "no stored refresh token is available to refresh the subscription",
            )

        try:
            token = await refresh_fn(entry, str(refresh_token))
        except UpstreamError:
            return await _mark_requires_reauth(
                session,
                credential,
                f"token refresh was rejected by provider "
                f"{entry.provider_id.value!r}",
            )

        new_access = token.get("access_token")
        if not new_access:
            return await _mark_requires_reauth(
                session,
                credential,
                "token refresh response did not include an access token",
            )

        expires_at = _expires_at_from_token(token)
        new_bundle = {
            "access_token": new_access,
            # Providers may rotate the refresh token on each refresh; store the new
            # one when present, otherwise retain the existing one.
            "refresh_token": token.get("refresh_token") or refresh_token,
            "account_id": bundle.get("account_id"),
            "scopes": token.get("scope") or token.get("scopes") or bundle.get("scopes"),
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
        }
        record = _encrypt_bundle(new_bundle, settings)
        secret.ciphertext = record.ciphertext
        secret.nonce = record.nonce
        secret.wrapped_dek = record.wrapped_dek
        secret.expires_at = expires_at

        # A successful refresh restores a previously reauth-flagged account (unless
        # the Operator has disabled it) and clears the recorded reason.
        if credential.status is CredentialStatus.REQUIRES_REAUTH:
            credential.status = CredentialStatus.ACTIVE
        credential.requires_reauth_reason = None
        await session.flush()

        return RefreshOutcome(
            account_id=account_id,
            refreshed=True,
            requires_reauth=False,
            reason=None,
            expires_at=expires_at,
        )


async def get_usable_token(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    refresh: RefreshFn | None = None,
    lock_factory: LockFactory | None = None,
) -> ProviderCredentialMaterial:
    """Return usable credential material, refreshing lazily when due (Req 3.1, 3.4).

    Loads the (non-deleted) credential and refuses immediately if it is marked as
    requiring reauthorization (Requirement 3.4) -- the Flow_Controller skips such
    accounts, and this is the defensive guard on the data path. For a
    Subscription_Account whose access token is within the configured renewal window
    (``GOZAR_SUBSCRIPTION_RENEWAL_WINDOW_SECONDS``) or already expired, it triggers a
    locked :func:`refresh_subscription` before returning the token, so a request never
    uses a stale token (Requirement 3.1). If that refresh fails the account is now
    flagged for reauthorization and a :class:`NoAvailableAccount` error is raised.

    For an API_Key_Account the stored key is decrypted and returned without any
    refresh. Raises :class:`NotFound` if the account does not exist or has been
    deleted, and :class:`NoAvailableAccount` when the account requires reauthorization.
    """
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    credential = await _load_live_credential(session, account_id)

    if credential.status is CredentialStatus.REQUIRES_REAUTH:
        raise NoAvailableAccount(
            f"subscription account {account_id} requires reauthorization and cannot "
            f"be used until it is reconnected"
        )

    if credential.kind is CredentialKind.API_KEY:
        secret = await session.get(ApiKeySecret, account_id)
        if secret is None:
            raise NotFound(f"API key secret for account {account_id} was not found")
        bundle = _decrypt_bundle_api_key(secret, settings)
        return ProviderCredentialMaterial(
            account_id=account_id,
            provider=credential.provider,
            kind=credential.kind,
            access_token=None,
            api_key=bundle.get("api_key"),
            provider_account_ref=credential.provider_account_ref,
            expires_at=None,
        )

    secret = await session.get(SubscriptionSecret, account_id)
    if secret is None:
        raise NotFound(f"subscription secret for account {account_id} was not found")

    renewal_window = timedelta(
        seconds=settings.subscription_renewal_window_seconds
    )
    if secret.expires_at is not None and refresh_needed(
        secret.expires_at, now, renewal_window
    ):
        outcome = await refresh_subscription(
            session,
            account_id,
            settings=settings,
            refresh=refresh,
            lock_factory=lock_factory,
        )
        if outcome.requires_reauth:
            raise NoAvailableAccount(
                f"subscription account {account_id} requires reauthorization after a "
                f"failed token refresh and cannot be used"
            )
        # Re-read the secret refreshed under the lock.
        secret = await session.get(SubscriptionSecret, account_id)
        if secret is None:  # pragma: no cover - defensive
            raise NotFound(
                f"subscription secret for account {account_id} was not found"
            )

    bundle = _decrypt_bundle(secret, settings)
    return ProviderCredentialMaterial(
        account_id=account_id,
        provider=credential.provider,
        kind=credential.kind,
        access_token=bundle.get("access_token"),
        api_key=None,
        provider_account_ref=bundle.get("account_id")
        or credential.provider_account_ref,
        expires_at=secret.expires_at,
    )


def _decrypt_bundle_api_key(
    secret: ApiKeySecret, settings: Settings
) -> dict[str, Any]:
    """Decrypt and JSON-decode an envelope-encrypted API-key bundle."""
    plaintext = decrypt(
        secret.ciphertext,
        secret.nonce,
        secret.wrapped_dek,
        settings=settings,
    )
    return json.loads(plaintext.decode("utf-8"))


__all__ = [
    "AccountView",
    "AuthorizationChallenge",
    "ConsumptionLookup",
    "DeviceAuthorizationChallenge",
    "DeviceAuthorizationOutcome",
    "DeviceCodePollFn",
    "DeviceCodeRequestFn",
    "LockFactory",
    "PendingConnectStore",
    "ProviderCredentialMaterial",
    "RedisPendingConnectStore",
    "RefreshFn",
    "RefreshOutcome",
    "begin_subscription_connect",
    "begin_device_subscription_connect",
    "complete_subscription_connect",
    "complete_device_subscription_connect",
    "connect_api_key",
    "delete",
    "get_usable_token",
    "list_accounts",
    "refresh_needed",
    "refresh_subscription",
    "set_enabled",
    "set_usage_limit",
]
