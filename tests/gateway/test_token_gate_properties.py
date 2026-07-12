"""Property-based tests for the Proxy_Gateway authentication gate (Property 13).

Validates Property 13 from the Gozar design: *for any* proxy request that presents no
Client_Token or an invalid/revoked one, the gateway rejects the request with an
authentication error (HTTP 401) and performs **zero** upstream Provider calls
(Requirement 6.2).

The pipeline authenticates the Client_Token in step 1 of the hot path
(:func:`gozar.gateway.pipeline.complete_chat_completion` /
:func:`gozar.gateway.pipeline.stream_chat_completion` -> ``_authenticate_and_route``).
A missing or invalid token must raise :class:`~gozar.core.errors.AuthError` *before*
the request ever reaches the injectable upstream seam.

To prove "no upstream call is made", every test injects a *recording* upstream seam
that appends to a call log and would otherwise return a perfectly valid response. The
property then asserts the log is empty across all generated missing/invalid/revoked
tokens: the gate denies before the network seam is ever consulted, so even a
fully-working upstream is never reached.

Following the project convention for DB-backed property tests (see
``tests/usage/test_counter_consistency_properties.py``), each Hypothesis example runs
against a fresh in-memory SQLite database and the in-memory Redis stand-in from this
package's ``conftest`` (``FakeRedis``), driven with ``asyncio.run`` because Hypothesis
drives a synchronous test function. No test touches the network, a real database, or a
real Redis.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Import every ORM model module so all tables register on Base.metadata before
# create_all (the pipeline opens/finalizes a Trace_Log, touching the usage tables).
from gozar.accounts import models as _accounts_models  # noqa: F401
from gozar.routing import models as _routing_models  # noqa: F401
from gozar.tokens import models as _tokens_models  # noqa: F401
from gozar.usage import models as _usage_models  # noqa: F401
from gozar.core.config import Settings
from gozar.core.db import Base
from gozar.core.errors import AuthError
from gozar.gateway.pipeline import complete_chat_completion, stream_chat_completion
from gozar.tokens.service import create_token, revoke
from gozar.translation.types import OpenAIChatMessage, OpenAIChatRequest

# Reuse this package's shared in-memory Redis stand-in and canonical responses.
from conftest import FakeRedis, openai_response

# Deterministic, well-formed 32-byte master key (base64) + test-only secrets. These
# are never real credentials; they only satisfy the pipeline's configuration.
_TEST_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _settings() -> Settings:
    """Build the test settings the pipeline needs (matches the package conftest)."""
    return Settings(
        master_key=_TEST_MASTER_KEY,
        token_pepper="property-13-test-pepper",
        jwt_secret="test-jwt-secret",
        redis_url="redis://localhost:6379/0",
        provider_base_urls={"openai": "https://api.openai.com/v1"},
    )


class _RecordingUpstream:
    """Non-streaming upstream seam that records every call it receives.

    If the gate ever lets a request through, ``calls`` becomes non-empty and the
    property fails. It returns a valid OpenAI response so a leak would otherwise
    "succeed" -- making the absence of a call the only thing keeping the test green.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, entry, material, adapter, body) -> dict[str, Any]:
        self.calls.append((entry, material, adapter, body))
        return openai_response()


class _RecordingStreamUpstream:
    """Streaming upstream seam that records every call it receives.

    Mirrors :class:`_RecordingUpstream` for the streaming path: invoking it records
    the call and returns a minimal but valid OpenAI SSE byte stream.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, entry, material, adapter, body) -> AsyncIterator[bytes]:
        self.calls.append((entry, material, adapter, body))

        async def _gen() -> AsyncIterator[bytes]:
            yield b"data: [DONE]\n\n"

        return _gen()


async def _acquire_must_not_run(session: AsyncSession, account_id) -> Any:
    """Credential acquirer that fails loudly if the gate is ever passed.

    Reaching credential acquisition already means the auth gate failed, so this raises
    rather than returning material -- a second, independent guard behind the upstream
    recorder.
    """
    raise AssertionError(
        "credential acquisition was reached for a missing/invalid token; "
        "the authentication gate must reject before routing"
    )


def _request(model: str = "gpt-4o", *, stream: bool = False) -> OpenAIChatRequest:
    """Build a minimal, valid inbound OpenAI chat completion request."""
    return OpenAIChatRequest(
        model=model,
        messages=[OpenAIChatMessage(role="user", content="hi")],
        stream=stream,
    )


async def _new_session() -> tuple[AsyncSession, Any]:
    """Create a fresh in-memory SQLite database and return ``(session, engine)``."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory(), engine


def _run(coro):
    """Execute an async coroutine to completion from a sync Hypothesis test."""
    return asyncio.run(coro)


# Hypothesis strategies -------------------------------------------------------

# A missing Client_Token: either entirely absent (``None``) or an empty/blank string,
# all of which the gate treats as "no token presented".
_missing_tokens = st.sampled_from([None, "", " ", "\t"])

# An invalid Client_Token. Two shapes are generated to exercise both rejection paths
# in ``verify``: arbitrary text that fails the ``gz-<prefix>-<secret>`` parse, and
# well-formed-but-unknown ``gz-`` strings that parse yet match no persisted row. Since
# no token is ever seeded for these examples, every value here is invalid.
_invalid_tokens = st.one_of(
    st.text(min_size=1, max_size=64),
    st.builds(
        lambda prefix, secret: f"gz-{prefix}-{secret}",
        st.text(alphabet="0123456789abcdef", min_size=1, max_size=16),
        st.text(
            alphabet=st.characters(min_codepoint=33, max_codepoint=126),
            min_size=1,
            max_size=43,
        ),
    ),
)

# Arbitrary model names + session ids, to confirm the gate is independent of the
# requested model and of session affinity.
_models = st.text(min_size=1, max_size=20)
_session_ids = st.one_of(st.none(), st.text(min_size=1, max_size=20))


# Feature: gozar, Property 13: For any proxy request that presents no Client_Token or
# an invalid/revoked one, the gateway rejects the request with an authentication error
# and performs zero upstream Provider calls.
@hyp_settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(token=_missing_tokens, model=_models, session_id=_session_ids)
def test_missing_token_never_reaches_upstream(token, model, session_id) -> None:
    """Validates: Requirements 6.2.

    A request presenting no Client_Token is rejected with a 401 ``AuthError`` and the
    non-streaming upstream seam is never invoked.
    """

    async def scenario() -> None:
        session, engine = await _new_session()
        upstream = _RecordingUpstream()
        try:
            with pytest.raises(AuthError) as excinfo:
                await complete_chat_completion(
                    session,
                    presented_token=token,
                    request=_request(model),
                    session_id=session_id,
                    redis=FakeRedis(),
                    settings=_settings(),
                    upstream=upstream,
                    acquire_material=_acquire_must_not_run,
                )
            assert excinfo.value.status_code == 401
            assert upstream.calls == []
        finally:
            await session.close()
            await engine.dispose()

    _run(scenario())


# Feature: gozar, Property 13: For any proxy request that presents no Client_Token or
# an invalid/revoked one, the gateway rejects the request with an authentication error
# and performs zero upstream Provider calls.
@hyp_settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(token=_invalid_tokens, model=_models, session_id=_session_ids)
def test_invalid_token_never_reaches_upstream(token, model, session_id) -> None:
    """Validates: Requirements 6.2.

    A request presenting an invalid Client_Token (malformed, or well-formed but
    matching no persisted token) is rejected with a 401 ``AuthError`` and the
    non-streaming upstream seam is never invoked. No token is seeded, so every
    generated value is genuinely invalid.
    """

    async def scenario() -> None:
        session, engine = await _new_session()
        upstream = _RecordingUpstream()
        try:
            with pytest.raises(AuthError) as excinfo:
                await complete_chat_completion(
                    session,
                    presented_token=token,
                    request=_request(model),
                    session_id=session_id,
                    redis=FakeRedis(),
                    settings=_settings(),
                    upstream=upstream,
                    acquire_material=_acquire_must_not_run,
                )
            assert excinfo.value.status_code == 401
            assert upstream.calls == []
        finally:
            await session.close()
            await engine.dispose()

    _run(scenario())


# Feature: gozar, Property 13: For any proxy request that presents no Client_Token or
# an invalid/revoked one, the gateway rejects the request with an authentication error
# and performs zero upstream Provider calls.
@hyp_settings(max_examples=80, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(label=st.text(min_size=0, max_size=40), model=_models)
def test_revoked_token_never_reaches_upstream(label, model) -> None:
    """Validates: Requirements 6.2.

    A real token presented *after revocation* is rejected with a 401 ``AuthError`` and
    the upstream seam is never invoked -- the "revoked one" arm of the property. The
    same secret is confirmed unusable here precisely because the gate denies it.
    """

    async def scenario() -> None:
        session, engine = await _new_session()
        settings = _settings()
        upstream = _RecordingUpstream()
        try:
            issued = await create_token(session, label, None, settings=settings)
            await revoke(session, issued.token_id)
            await session.flush()

            with pytest.raises(AuthError) as excinfo:
                await complete_chat_completion(
                    session,
                    presented_token=issued.secret,
                    request=_request(model),
                    redis=FakeRedis(),
                    settings=settings,
                    upstream=upstream,
                    acquire_material=_acquire_must_not_run,
                )
            assert excinfo.value.status_code == 401
            assert upstream.calls == []
        finally:
            await session.close()
            await engine.dispose()

    _run(scenario())


# Feature: gozar, Property 13: For any proxy request that presents no Client_Token or
# an invalid/revoked one, the gateway rejects the request with an authentication error
# and performs zero upstream Provider calls.
@hyp_settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(
    token=st.one_of(_missing_tokens, _invalid_tokens),
    model=_models,
    session_id=_session_ids,
)
def test_streaming_missing_or_invalid_token_never_reaches_upstream(
    token, model, session_id
) -> None:
    """Validates: Requirements 6.2.

    The streaming hot path shares the same gate: a missing or invalid Client_Token
    raises a 401 ``AuthError`` synchronously (before the SSE iterator is produced) and
    the streaming upstream seam is never invoked.
    """

    async def scenario() -> None:
        session, engine = await _new_session()
        stream_upstream = _RecordingStreamUpstream()
        try:
            with pytest.raises(AuthError) as excinfo:
                await stream_chat_completion(
                    session,
                    presented_token=token,
                    request=_request(model, stream=True),
                    session_id=session_id,
                    redis=FakeRedis(),
                    settings=_settings(),
                    stream_upstream=stream_upstream,
                    acquire_material=_acquire_must_not_run,
                )
            assert excinfo.value.status_code == 401
            assert stream_upstream.calls == []
        finally:
            await session.close()
            await engine.dispose()

    _run(scenario())
