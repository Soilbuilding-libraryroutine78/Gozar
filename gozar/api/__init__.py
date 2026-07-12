"""Admin REST router (control-path); wires modules via dependency injection.

This package exposes the administrative HTTP surface under ``/api``. Each domain has
its own router module (:mod:`gozar.api.auth`, :mod:`gozar.api.accounts`,
:mod:`gozar.api.tokens`, :mod:`gozar.api.chains`, :mod:`gozar.api.traces`,
:mod:`gozar.api.analytics`), and :data:`api_router` aggregates them under the shared
``/api`` prefix for mounting in :func:`gozar.app.create_app`.

The :mod:`gozar.api.auth` router (login / refresh / first-run bootstrap) is public by
necessity - login and bootstrap cannot require an existing session. Every other route
across these modules is guarded by the fail-closed
:func:`gozar.auth.rbac.require` dependency with the appropriate
:class:`~gozar.auth.rbac.Permission`, the DB session is provided via the
:func:`gozar.core.db.get_session` dependency, and request/response models exclude all
secret values (the only exceptions are API key creation, password-confirmed reveal,
and explicit password-confirmed rotation).
"""

from __future__ import annotations

from fastapi import APIRouter

from gozar.api.accounts import router as accounts_router
from gozar.api.analytics import router as analytics_router
from gozar.api.auth import router as auth_router
from gozar.api.chains import router as chains_router
from gozar.api.models import router as models_router
from gozar.api.tokens import router as tokens_router
from gozar.api.traces import router as traces_router

# Aggregated admin control-path router, mounted under ``/api`` by the app assembly.
# The auth router (login/refresh/bootstrap) is public by necessity; every other
# router below is guarded by the fail-closed require(...) dependency.
api_router = APIRouter(prefix="/api")
api_router.include_router(auth_router)
api_router.include_router(accounts_router)
api_router.include_router(tokens_router)
api_router.include_router(chains_router)
api_router.include_router(models_router)
api_router.include_router(traces_router)
api_router.include_router(analytics_router)

__all__ = ["api_router"]
