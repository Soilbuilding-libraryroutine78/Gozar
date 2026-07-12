"""Async Redis client.

Provides the process-wide ``redis.asyncio`` client used for the consumption
counters, refresh locks, and the session-affinity map. The Redis URL is always read
from :mod:`gozar.core.config` (``GOZAR_REDIS_URL``); there are no hardcoded URLs.

The factory follows the same lazy-cached, fail-closed pattern as
:mod:`gozar.core.db`: it raises ``RuntimeError`` when the URL is not configured
rather than silently constructing an unusable client. Clients decode responses to
``str`` so callers work with text values directly.
"""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis

from gozar.core.config import Settings, get_settings


def build_redis(settings: Settings) -> Redis:
    """Create an async Redis client from the given settings.

    Raises ``RuntimeError`` (fail closed) when the Redis URL is not configured,
    rather than returning an unusable client.
    """
    if not settings.redis_url:
        raise RuntimeError(
            "GOZAR_REDIS_URL is not configured; the Redis layer cannot start."
        )
    return Redis.from_url(settings.redis_url, decode_responses=True)


@lru_cache
def get_redis() -> Redis:
    """Return the process-wide async Redis client (lazily created, cached)."""
    return build_redis(get_settings())


async def dispose_redis() -> None:
    """Close the cached Redis client and clear the cache.

    Intended for application shutdown and test teardown so connections are released
    cleanly.
    """
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
    get_redis.cache_clear()
