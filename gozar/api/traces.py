"""Admin control-path router for Trace_Log viewing (Requirement 14.3).

Exposes a paginated trace list and a per-trace detail view carrying the inbound
metadata, the selected Upstream_Credential, the outcome, and the elapsed duration.
Every route is guarded by the fail-closed :func:`gozar.auth.rbac.require` dependency
with :data:`~gozar.auth.rbac.Permission.VIEW_TRACES` (Requirements 16.1, 16.3). Trace
metadata never contains secret material (Requirement 16.4).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.api.schemas import TraceDetailResponse, TraceSummaryResponse
from gozar.auth.rbac import Identity, Permission, require
from gozar.core.db import get_session
from gozar.usage.service import get_trace, list_traces

router = APIRouter(prefix="/traces", tags=["traces"])

# Single fail-closed guard shared by every route in this router.
_guard = require(Permission.VIEW_TRACES)


@router.get(
    "", summary="List request traces", response_model=list[TraceSummaryResponse]
)
async def list_traces_route(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> list[TraceSummaryResponse]:
    """Return recent Trace_Log entries, most recent first (Requirement 14.3)."""
    traces = await list_traces(session, limit=limit, offset=offset)
    return [TraceSummaryResponse.from_trace(trace) for trace in traces]


@router.get(
    "/{correlation_id}",
    summary="Get a single request trace",
    response_model=TraceDetailResponse,
)
async def get_trace_route(
    correlation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> TraceDetailResponse:
    """Return a single trace with full metadata or 404 (Requirement 14.3)."""
    trace = await get_trace(session, correlation_id)
    return TraceDetailResponse.from_trace(trace)


__all__ = ["router"]
