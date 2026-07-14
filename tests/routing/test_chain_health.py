"""Pure chain health checks against current account model snapshots."""

import uuid
from datetime import datetime, timezone

from gozar.accounts.models import CredentialKind, CredentialStatus
from gozar.accounts.service import AccountView
from gozar.routing.health import assess_chain_health
from gozar.routing.chains import RouteKind
from gozar.routing.service import ChainEntryView, ChainView


def _account(
    account_id: uuid.UUID,
    label: str,
    *,
    provider: str = "openrouter",
) -> AccountView:
    return AccountView(
        account_id=account_id,
        provider=provider,
        kind=(
            CredentialKind.SUBSCRIPTION
            if provider in {"codex", "anthropic"}
            else CredentialKind.API_KEY
        ),
        label=label,
        status=CredentialStatus.ACTIVE,
        connected_at=datetime.now(timezone.utc),
        limit=None,
        consumption=0,
    )


def test_removed_fallback_model_marks_chain_for_review() -> None:
    primary_id, fallback_id = uuid.uuid4(), uuid.uuid4()
    chain = ChainView(
        chain_id=uuid.uuid4(),
        name="production",
        client_key="production",
        model_selector=None,
        entries=(
            ChainEntryView(primary_id, 0, "openai/gpt-5.4-mini"),
            ChainEntryView(fallback_id, 1, "google/gemini-2"),
        ),
    )
    accounts = {
        primary_id: _account(primary_id, "Primary"),
        fallback_id: _account(fallback_id, "Fallback"),
    }

    health = assess_chain_health(
        chain,
        accounts,
        {
            primary_id: frozenset({"openai/gpt-5.4-mini"}),
            fallback_id: frozenset({"google/gemini-2.5-flash"}),
        },
    )

    assert health.status == "warning"
    assert health.issues[0].code == "model_unavailable"
    assert health.issues[0].position == 1


def test_chain_with_no_usable_nodes_is_broken() -> None:
    account_id = uuid.uuid4()
    chain = ChainView(
        chain_id=uuid.uuid4(),
        name="expired",
        client_key=None,
        model_selector=None,
        entries=(ChainEntryView(account_id, 0, "retired-model"),),
    )

    health = assess_chain_health(
        chain,
        {account_id: _account(account_id, "Only account")},
        {account_id: frozenset({"current-model"})},
    )

    assert health.status == "broken"
    assert [issue.code for issue in health.issues] == ["model_unavailable"]


def test_embedding_lane_reports_unsupported_subscription_provider() -> None:
    account_id = uuid.uuid4()
    chain = ChainView(
        chain_id=uuid.uuid4(),
        name="invalid-embedding-route",
        client_key=None,
        model_selector=None,
        entries=(
            ChainEntryView(
                account_id,
                0,
                "text-embedding-3-small",
                route_kind=RouteKind.EMBEDDINGS,
            ),
        ),
    )

    health = assess_chain_health(
        chain,
        {account_id: _account(account_id, "Codex", provider="codex")},
        {account_id: frozenset()},
    )

    assert health.status == "broken"
    assert health.issues[0].code == "route_unsupported"
    assert health.issues[0].route_kind is RouteKind.EMBEDDINGS


def test_embedding_lane_uses_its_own_discovered_model_catalog() -> None:
    account_id = uuid.uuid4()
    chain = ChainView(
        chain_id=uuid.uuid4(),
        name="rag",
        client_key=None,
        model_selector=None,
        entries=(
            ChainEntryView(
                account_id,
                0,
                "text-embedding-3-small",
                route_kind=RouteKind.EMBEDDINGS,
            ),
        ),
    )

    health = assess_chain_health(
        chain,
        {account_id: _account(account_id, "OpenAI", provider="openai")},
        {account_id: frozenset({"gpt-5.4-mini"})},
        {account_id: frozenset({"text-embedding-3-small"})},
    )

    assert health.status == "healthy"
    assert health.issues == ()
