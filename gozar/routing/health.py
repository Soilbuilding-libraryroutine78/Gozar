"""Pure health assessment for persisted provider/model routing chains."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from gozar.accounts.models import CredentialStatus
from gozar.accounts.service import AccountView
from gozar.providers.registry import provider_supports_embeddings
from gozar.routing.chains import RouteKind
from gozar.routing.service import ChainView


@dataclass(frozen=True)
class ChainHealthIssue:
    """One actionable chain configuration issue."""

    code: str
    message: str
    position: int | None = None
    account_id: UUID | None = None
    model_id: str | None = None
    route_kind: RouteKind | None = None


@dataclass(frozen=True)
class ChainHealth:
    """Current health of a saved chain against account and model snapshots."""

    status: str
    issues: tuple[ChainHealthIssue, ...]


def assess_chain_health(
    chain: ChainView,
    accounts: Mapping[UUID, AccountView],
    chat_models_by_account: Mapping[UUID, frozenset[str]],
    embedding_models_by_account: Mapping[UUID, frozenset[str]] | None = None,
) -> ChainHealth:
    """Assess a chain without I/O using current account/model snapshots."""

    if not chain.entries:
        return ChainHealth(
            status="broken",
            issues=(
                ChainHealthIssue(
                    code="empty_chain",
                    message="This chain has no routing nodes.",
                ),
            ),
        )

    issues: list[ChainHealthIssue] = []
    usable_nodes = 0
    for entry in chain.entries:
        step = entry.position + 1
        route_label = "Embedding" if entry.route_kind is RouteKind.EMBEDDINGS else "LLM"
        account = accounts.get(entry.account_id)
        if account is None:
            issues.append(
                ChainHealthIssue(
                    code="account_missing",
                    message=(
                        f"{route_label} step {step} references an account that no "
                        "longer exists."
                    ),
                    position=entry.position,
                    account_id=entry.account_id,
                    model_id=entry.model_id,
                    route_kind=entry.route_kind,
                )
            )
            continue
        if account.status is not CredentialStatus.ACTIVE:
            issues.append(
                ChainHealthIssue(
                    code="account_unavailable",
                    message=(
                        f"{route_label} step {step} uses {account.label}, which is "
                        f"{account.status.value.replace('_', ' ')}."
                    ),
                    position=entry.position,
                    account_id=entry.account_id,
                    model_id=entry.model_id,
                    route_kind=entry.route_kind,
                )
            )
            continue

        if (
            entry.route_kind is RouteKind.EMBEDDINGS
            and not provider_supports_embeddings(account.provider)
        ):
            issues.append(
                ChainHealthIssue(
                    code="route_unsupported",
                    message=(
                        f"Embedding step {step} uses {account.label}, but "
                        f"{account.provider} does not provide embeddings."
                    ),
                    position=entry.position,
                    account_id=entry.account_id,
                    model_id=entry.model_id,
                    route_kind=entry.route_kind,
                )
            )
            continue

        models_by_account = (
            embedding_models_by_account
            if entry.route_kind is RouteKind.EMBEDDINGS
            and embedding_models_by_account is not None
            else chat_models_by_account
        )
        model_ids = models_by_account.get(entry.account_id, frozenset())
        if entry.model_id is None:
            usable_nodes += 1
            if entry.position > 0:
                issues.append(
                    ChainHealthIssue(
                        code="dynamic_fallback_model",
                        message=(
                            f"{route_label} step {step} reuses the request model; choose a "
                            f"{account.provider} "
                            "model when the providers expose different model ids."
                        ),
                        position=entry.position,
                        account_id=entry.account_id,
                        route_kind=entry.route_kind,
                    )
                )
            continue

        if not model_ids:
            usable_nodes += 1
            issues.append(
                ChainHealthIssue(
                    code="catalog_unavailable",
                    message=(
                        f"{route_label} step {step} uses {entry.model_id}, but "
                        f"{account.provider} did not "
                        "advertise a model catalog to verify it."
                    ),
                    position=entry.position,
                    account_id=entry.account_id,
                    model_id=entry.model_id,
                    route_kind=entry.route_kind,
                )
            )
            continue

        if entry.model_id not in model_ids:
            issues.append(
                ChainHealthIssue(
                    code="model_unavailable",
                    message=(
                        f"{route_label} step {step} model {entry.model_id} is no longer "
                        "advertised by "
                        f"{account.label}."
                    ),
                    position=entry.position,
                    account_id=entry.account_id,
                    model_id=entry.model_id,
                    route_kind=entry.route_kind,
                )
            )
            continue

        usable_nodes += 1

    if usable_nodes == 0:
        status = "broken"
    elif issues:
        status = "warning"
    else:
        status = "healthy"
    return ChainHealth(status=status, issues=tuple(issues))


__all__ = ["ChainHealth", "ChainHealthIssue", "assess_chain_health"]
