"""Admin control-path router for the Analytics_Service (Requirement 15).

Exposes per-token, per-account, and system reports over a caller-supplied half-open
``[start, end)`` UTC time range. Every route is guarded by the fail-closed
:func:`gozar.auth.rbac.require` dependency with
:data:`~gozar.auth.rbac.Permission.VIEW_ANALYTICS` (Requirements 16.1, 16.3). Reports
are pure aggregates of usage/trace data and carry no secret material.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.analytics.service import (
    TimeRange,
    account_report,
    system_report,
    token_report,
)
from gozar.api.schemas import (
    AccountAnalyticsResponse,
    SystemAnalyticsResponse,
    TokenAnalyticsResponse,
)
from gozar.auth.rbac import Identity, Permission, require
from gozar.core.db import get_session

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Single fail-closed guard shared by every route in this router.
_guard = require(Permission.VIEW_ANALYTICS)


@router.get(
    "/system",
    summary="System-wide analytics report",
    response_model=SystemAnalyticsResponse,
)
async def system_report_route(
    start: datetime = Query(..., description="Range start (inclusive, UTC)."),
    end: datetime = Query(..., description="Range end (exclusive, UTC)."),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> SystemAnalyticsResponse:
    """Aggregate system-wide usage over the range (Requirement 15.3)."""
    report = await system_report(session, TimeRange(start=start, end=end))
    return SystemAnalyticsResponse.from_report(report)


@router.get(
    "/tokens/{token_id}",
    summary="Per-token analytics report",
    response_model=TokenAnalyticsResponse,
)
async def token_report_route(
    token_id: uuid.UUID,
    start: datetime = Query(..., description="Range start (inclusive, UTC)."),
    end: datetime = Query(..., description="Range end (exclusive, UTC)."),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> TokenAnalyticsResponse:
    """Aggregate a Gozar API key's usage over the range (Requirement 15.1)."""
    report = await token_report(session, token_id, TimeRange(start=start, end=end))
    return TokenAnalyticsResponse.from_report(report)


@router.get(
    "/accounts/{account_id}",
    summary="Per-account analytics report",
    response_model=AccountAnalyticsResponse,
)
async def account_report_route(
    account_id: uuid.UUID,
    start: datetime = Query(..., description="Range start (inclusive, UTC)."),
    end: datetime = Query(..., description="Range end (exclusive, UTC)."),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> AccountAnalyticsResponse:
    """Aggregate an Upstream_Credential's usage over the range (Requirement 15.2)."""
    report = await account_report(session, account_id, TimeRange(start=start, end=end))
    return AccountAnalyticsResponse.from_report(report)


__all__ = ["router"]
