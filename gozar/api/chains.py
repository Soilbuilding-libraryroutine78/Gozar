"""Admin control-path router for the Flow_Controller's Fallback_Chains.

Exposes Fallback_Chain CRUD (create, list, read, edit, delete) to the Web_Console's
visual chain editor. Every route is guarded by the fail-closed
:func:`gozar.auth.rbac.require` dependency with
:data:`~gozar.auth.rbac.Permission.MANAGE_CHAINS` (Requirements 16.1, 16.3). Chains
carry no secret material (Requirements 10.1, 10.4).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.api.schemas import (
    ChainResponse,
    CreateChainRequest,
    EditChainRequest,
    UpsertChainRequest,
)
from gozar.auth.rbac import Identity, Permission, require
from gozar.core.db import get_session
from gozar.routing.service import (
    ChainEntryInput,
    create_chain,
    delete_chain,
    edit_chain,
    get_chain,
    list_chains,
    upsert_chain_by_key,
)

router = APIRouter(prefix="/chains", tags=["chains"])

# Single fail-closed guard shared by every route in this router.
_guard = require(Permission.MANAGE_CHAINS)


def _entry_inputs(payload: CreateChainRequest | EditChainRequest) -> list[ChainEntryInput] | None:
    """Map validated transport nodes to the routing service input type."""

    entries = payload.resolved_entries
    if entries is None:
        return None
    return [
        ChainEntryInput(
            account_id=entry.account_id,
            model_id=entry.model,
            fallback_policy=entry.fallback_policy,
        )
        for entry in entries
    ]


@router.get("", summary="List fallback chains", response_model=list[ChainResponse])
async def list_chains_route(
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> list[ChainResponse]:
    """Return all Fallback_Chains as secret-free views."""
    views = await list_chains(session)
    return [ChainResponse.from_view(view) for view in views]


@router.post(
    "",
    summary="Create a fallback chain",
    response_model=ChainResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_chain_route(
    payload: CreateChainRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ChainResponse:
    """Create a chain with its ordered entries (Requirement 10.1)."""
    view = await create_chain(
        session,
        payload.name,
        _entry_inputs(payload) or [],
        model_selector=payload.model_selector,
    )
    return ChainResponse.from_view(view)


@router.put(
    "/by-key/{client_key}",
    summary="Idempotently create or replace a fallback chain",
    response_model=ChainResponse,
)
async def upsert_chain_by_key_route(
    payload: UpsertChainRequest,
    client_key: str = Path(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ChainResponse:
    """Upsert a stable chain resource for programmatic integrations."""

    view = await upsert_chain_by_key(
        session,
        client_key,
        payload.name,
        _entry_inputs(payload) or [],
        model_selector=payload.model_selector,
    )
    return ChainResponse.from_view(view)


@router.get(
    "/{chain_id}", summary="Get a fallback chain", response_model=ChainResponse
)
async def get_chain_route(
    chain_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ChainResponse:
    """Return a single chain or 404 if it does not exist."""
    view = await get_chain(session, chain_id)
    return ChainResponse.from_view(view)


@router.put(
    "/{chain_id}", summary="Edit a fallback chain", response_model=ChainResponse
)
async def edit_chain_route(
    chain_id: uuid.UUID,
    payload: EditChainRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ChainResponse:
    """Edit a chain's name, ordered entries, and/or model selector (Req 10.4).

    Only the fields explicitly present in the request body are changed. An explicit
    ``model_selector: null`` clears the selector while omitting it leaves it
    unchanged.
    """
    fields = payload.model_fields_set
    kwargs: dict = {}
    if "name" in fields:
        kwargs["name"] = payload.name
    if "entries" in fields or "account_ids" in fields:
        kwargs["account_ids"] = _entry_inputs(payload) or []
    if "model_selector" in fields:
        kwargs["model_selector"] = payload.model_selector
    view = await edit_chain(session, chain_id, **kwargs)
    return ChainResponse.from_view(view)


@router.delete(
    "/{chain_id}",
    summary="Delete a fallback chain",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_chain_route(
    chain_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Delete a chain and its entries."""
    await delete_chain(session, chain_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
