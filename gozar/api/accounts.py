"""Admin control-path router for the Account_Manager (Requirement 5.4).

Exposes the Upstream_Credential lifecycle to the Web_Console and programmatic admin
clients: list accounts, connect subscription accounts (OAuth + PKCE) and API-key
accounts, configure usage limits, enable/disable, and delete. Every route is guarded
by the fail-closed :func:`gozar.auth.rbac.require` dependency with
:data:`~gozar.auth.rbac.Permission.MANAGE_ACCOUNTS` so an unauthenticated request is
rejected with 401 and an authenticated-but-unauthorized one with 403 before any
handler logic runs (Requirements 16.1, 16.3).

Responses never carry secret material; the connect flows return only a non-secret
credential summary, and the PKCE verifier is held server-side (Requirements 5.4,
16.2, 16.4).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.service import (
    begin_device_subscription_connect,
    begin_subscription_connect,
    complete_device_subscription_connect,
    complete_subscription_connect,
    connect_api_key,
    delete,
    list_accounts,
    set_enabled,
    set_usage_limit,
)
from gozar.api.schemas import (
    AccountResponse,
    ApiKeyConnectRequest,
    AuthorizationChallengeResponse,
    CredentialSummaryResponse,
    DeviceAuthorizationChallengeResponse,
    DeviceAuthorizationCompleteResponse,
    SetEnabledRequest,
    SubscriptionBeginRequest,
    SubscriptionCompleteRequest,
    SubscriptionDeviceBeginRequest,
    SubscriptionDeviceCompleteRequest,
)
from gozar.auth.rbac import Identity, Permission, require
from gozar.core.db import get_session
from gozar.core.logging import get_logger
from gozar.core.redis import get_redis
from gozar.usage.limits import UsageLimitSpec
from gozar.usage.service import SUBJECT_ACCOUNT, read_subject_consumption

router = APIRouter(prefix="/accounts", tags=["accounts"])

_logger = get_logger(__name__)

# Single fail-closed guard shared by every route in this router.
_guard = require(Permission.MANAGE_ACCOUNTS)


async def _account_consumption(
    account_id: uuid.UUID, limit: UsageLimitSpec | None
) -> float:
    """Read an account's recorded consumption from the Usage_Recorder counters.

    Reads the same Redis counters the Proxy_Gateway enforces limits against (via
    :func:`gozar.usage.service.read_subject_consumption`). The counter store is a
    metering side-channel, not the source of truth, so if it is unavailable
    (unconfigured or unreachable) the listing degrades to ``0.0`` rather than failing
    the operator's accounts view; the condition is logged for observability.
    """
    try:
        return await read_subject_consumption(
            get_redis(), SUBJECT_ACCOUNT, account_id, limit
        )
    except (RedisError, RuntimeError, OSError) as exc:
        _logger.warning(
            "usage counter unavailable; reporting account consumption as 0.0",
            extra={"account_id": str(account_id), "error": str(exc)},
        )
        return 0.0


@router.get("", summary="List connected accounts", response_model=list[AccountResponse])
async def list_accounts_route(
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> list[AccountResponse]:
    """Return non-secret summaries of every connected account (Requirement 5.4)."""
    views = await list_accounts(session, consumption_lookup=_account_consumption)
    return [AccountResponse.from_view(view) for view in views]


@router.post(
    "/connect/api-key",
    summary="Connect a metered API-key account",
    response_model=CredentialSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_api_key_route(
    payload: ApiKeyConnectRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> CredentialSummaryResponse:
    """Validate and connect an API key (Requirements 2.1-2.3)."""
    credential = await connect_api_key(
        session, payload.provider, payload.api_key, label=payload.label
    )
    return CredentialSummaryResponse.from_credential(credential)


@router.post(
    "/connect/subscription/begin",
    summary="Begin a subscription OAuth connect",
    response_model=AuthorizationChallengeResponse,
)
async def begin_subscription_route(
    payload: SubscriptionBeginRequest,
    identity: Identity = Depends(_guard),
) -> AuthorizationChallengeResponse:
    """Start a subscription connect and return the authorize URL (Requirement 1.1)."""
    challenge = await begin_subscription_connect(payload.provider)
    return AuthorizationChallengeResponse.from_challenge(challenge)


@router.post(
    "/connect/subscription/device/begin",
    summary="Begin a subscription device-code connect",
    response_model=DeviceAuthorizationChallengeResponse,
)
async def begin_device_subscription_route(
    payload: SubscriptionDeviceBeginRequest,
    identity: Identity = Depends(_guard),
) -> DeviceAuthorizationChallengeResponse:
    """Start device-code subscription connect for supported providers."""
    challenge = await begin_device_subscription_connect(payload.provider)
    return DeviceAuthorizationChallengeResponse.from_challenge(challenge)


@router.post(
    "/connect/subscription/device/complete",
    summary="Poll and complete a subscription device-code connect",
    response_model=DeviceAuthorizationCompleteResponse,
)
async def complete_device_subscription_route(
    payload: SubscriptionDeviceCompleteRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> DeviceAuthorizationCompleteResponse:
    """Poll a device-code connect and create the credential once approved."""
    outcome = await complete_device_subscription_connect(
        session,
        payload.pending_id,
        label=payload.label,
    )
    if outcome.pending:
        return DeviceAuthorizationCompleteResponse(status="pending")
    assert outcome.credential is not None  # for type-checkers; pending handled above
    return DeviceAuthorizationCompleteResponse(
        status="connected",
        account=CredentialSummaryResponse.from_credential(outcome.credential),
    )


@router.post(
    "/connect/subscription/complete",
    summary="Complete a subscription OAuth connect",
    response_model=CredentialSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def complete_subscription_route(
    payload: SubscriptionCompleteRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> CredentialSummaryResponse:
    """Exchange the provider callback for an encrypted account (Requirement 1.3)."""
    credential = await complete_subscription_connect(
        session,
        payload.pending_id,
        payload.code,
        payload.state,
        label=payload.label,
    )
    return CredentialSummaryResponse.from_credential(credential)


@router.put(
    "/{account_id}/limit",
    summary="Set an account usage limit",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_account_limit_route(
    account_id: uuid.UUID,
    limit: UsageLimitSpec,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Persist (create or replace) an account's Usage_Limit (Requirements 4.1, 4.4)."""
    await set_usage_limit(session, account_id, limit)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{account_id}/enabled",
    summary="Enable or disable an account",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_account_enabled_route(
    account_id: uuid.UUID,
    payload: SetEnabledRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Enable or disable a credential (Requirements 5.1, 5.2)."""
    await set_enabled(session, account_id, payload.enabled)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{account_id}",
    summary="Delete an account (hard-delete secrets, retain history)",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_account_route(
    account_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Delete a credential, removing secrets but keeping usage history (Req 5.3)."""
    await delete(session, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
