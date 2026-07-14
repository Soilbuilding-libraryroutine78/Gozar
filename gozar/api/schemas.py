"""Request/response models for the admin control-path API.

Every list/read model here is **secret-free**: no model carries an Upstream_Credential
secret, a Subscription_Token, a credential ciphertext, or a Gozar API key secret
value. The deliberate exception is :class:`IssuedTokenResponse`, which returns an API
key secret from create, password-confirmed reveal, or explicit rotation.

The models map to/from the service-layer value objects (the ``*View`` dataclasses and
the analytics dataclasses) so the routers stay thin: they validate input with these
models, call the owning service function, and render the result with these models.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from gozar.accounts.service import (
    AccountView,
    AuthorizationChallenge,
    DeviceAuthorizationChallenge,
)
from gozar.accounts.models import UpstreamCredential
from gozar.analytics.service import (
    AccountAnalytics,
    LimitConsumption,
    SystemAnalytics,
    TimeRange,
    TokenAnalytics,
    TokenCounts,
)
from gozar.providers.model_catalog import ProviderModelCatalogView
from gozar.routing.chains import FallbackPolicy, RouteKind
from gozar.routing.service import ChainView
from gozar.tokens.service import IssuedToken, TokenView
from gozar.translation.types import OpenAIModelCard
from gozar.usage.limits import UsageLimitSpec
from gozar.usage.models import TraceLog
from gozar.usage.service import trace_elapsed

# ---------------------------------------------------------------------------
# Accounts (Account_Manager) -- Requirement 5.4
# ---------------------------------------------------------------------------


class ApiKeyConnectRequest(BaseModel):
    """Connect a metered API-key account (Requirements 2.1-2.3)."""

    provider: str = Field(..., description="Provider id, e.g. 'openai' or 'openrouter'.")
    api_key: str = Field(..., description="The metered API key to validate and store.")
    label: str | None = Field(
        default=None, description="Optional operator-facing label for the account."
    )


class SubscriptionBeginRequest(BaseModel):
    """Begin a subscription OAuth + PKCE connect (Requirement 1.1)."""

    provider: str = Field(..., description="Subscription provider id, e.g. 'codex'.")


class SubscriptionDeviceBeginRequest(SubscriptionBeginRequest):
    """Begin a subscription device-code connect for supported providers."""


class SubscriptionDeviceCompleteRequest(BaseModel):
    """Poll/complete a device-code subscription connect."""

    pending_id: str = Field(..., description="Opaque handle from the device begin step.")
    label: str | None = Field(default=None, description="Optional account label.")


class SubscriptionCompleteRequest(BaseModel):
    """Complete a subscription connect with the provider callback (Requirement 1.3).

    ``code`` may be either a bare authorization code OR the **full redirect URL** the
    Operator copied from their browser's address bar (e.g.
    ``http://localhost:1455/auth/callback?code=...&state=...``); the backend extracts
    the code (and state) from it. ``state`` is therefore **optional**: omit it and let
    the backend recover it from a pasted redirect URL, or send it explicitly. When no
    state is available at all (a bare code was pasted), the completion is keyed solely
    by the opaque, unguessable ``pending_id`` -- the manual-paste fallback that keeps
    the flow domain-independent.
    """

    pending_id: str = Field(..., description="Opaque handle from the begin step.")
    code: str = Field(
        ...,
        description=(
            "Authorization code, or the full redirect URL pasted from the browser "
            "(the backend extracts the code and state from it)."
        ),
    )
    state: str | None = Field(
        default=None,
        description=(
            "Optional anti-CSRF state. May be omitted; the backend recovers it from a "
            "pasted redirect URL when present."
        ),
    )
    label: str | None = Field(default=None, description="Optional account label.")


class SetEnabledRequest(BaseModel):
    """Enable or disable a credential or token (Requirements 5.1, 5.2, 9.3, 9.4)."""

    enabled: bool = Field(..., description="True to enable, false to disable.")


class AuthorizationChallengeResponse(BaseModel):
    """The authorize URL and opaque handles returned when a connect begins.

    Carries no secret: the PKCE ``code_verifier`` is held server-side and never
    leaves Gozar (Requirement 1.1).
    """

    pending_id: str
    authorize_url: str
    state: str

    @classmethod
    def from_challenge(
        cls, challenge: AuthorizationChallenge
    ) -> "AuthorizationChallengeResponse":
        return cls(
            pending_id=challenge.pending_id,
            authorize_url=challenge.authorize_url,
            state=challenge.state,
        )


class DeviceAuthorizationChallengeResponse(BaseModel):
    """One-time device-code challenge returned by supported subscription providers."""

    pending_id: str
    verification_url: str
    user_code: str
    interval_seconds: int

    @classmethod
    def from_challenge(
        cls, challenge: DeviceAuthorizationChallenge
    ) -> "DeviceAuthorizationChallengeResponse":
        return cls(
            pending_id=challenge.pending_id,
            verification_url=challenge.verification_url,
            user_code=challenge.user_code,
            interval_seconds=challenge.interval_seconds,
        )


class AccountResponse(BaseModel):
    """A non-secret summary of a connected Upstream_Credential (Requirement 5.4)."""

    account_id: uuid.UUID
    provider: str
    kind: str
    label: str
    status: str
    connected_at: datetime
    limit: UsageLimitSpec | None = None
    consumption: float = 0.0

    @classmethod
    def from_view(cls, view: AccountView) -> "AccountResponse":
        return cls(
            account_id=view.account_id,
            provider=view.provider,
            kind=view.kind.value,
            label=view.label,
            status=view.status.value,
            connected_at=view.connected_at,
            limit=view.limit,
            consumption=view.consumption,
        )


class CredentialSummaryResponse(BaseModel):
    """A minimal, secret-free summary of a just-connected credential."""

    account_id: uuid.UUID
    provider: str
    kind: str
    label: str
    status: str

    @classmethod
    def from_credential(
        cls, credential: UpstreamCredential
    ) -> "CredentialSummaryResponse":
        return cls(
            account_id=credential.id,
            provider=credential.provider,
            kind=credential.kind.value,
            label=credential.label,
            status=credential.status.value,
        )


class DeviceAuthorizationCompleteResponse(BaseModel):
    """Polling response for a device-code subscription connect."""

    status: str = Field(..., description="'pending' until approved, then 'connected'.")
    account: CredentialSummaryResponse | None = None


# ---------------------------------------------------------------------------
# Tokens (Token_Authority) -- Requirement 8.3
# ---------------------------------------------------------------------------


class CreateTokenRequest(BaseModel):
    """Create a Gozar API key (Requirement 8.1)."""

    label: str = Field(..., description="Operator-facing label for the token.")
    limit: UsageLimitSpec | None = Field(
        default=None, description="Optional usage limit to attach at creation."
    )
    assigned_chain_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional Fallback_Chain id this API key should route through. "
            "Null keeps model-selector auto routing."
        ),
    )


class SetTokenChainRequest(BaseModel):
    """Assign or clear the Fallback_Chain used by a Gozar API key."""

    assigned_chain_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Fallback_Chain id to pin this API key to, or null for auto routing."
        ),
    )


class RevealTokenRequest(BaseModel):
    """Confirm password-protected reveal for an existing Gozar API key."""

    password: str = Field(
        ...,
        min_length=1,
        description="Current operator password used to reveal the API key.",
    )
    existing_api_key: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional full existing API key. Used only for legacy keys that were "
            "created before encrypted reveal storage; it is verified and stored "
            "encrypted without generating a replacement."
        ),
    )


class RotateTokenRequest(RevealTokenRequest):
    """Confirm destructive replacement issuance for a Gozar API key."""


class IssuedTokenResponse(BaseModel):
    """The result of creating/revealing/rotating an API key.

    The ``secret`` is the full presentable token string. It is never returned by
    list/read endpoints; reveal requires operator password confirmation.
    """

    token_id: uuid.UUID
    id_prefix: str
    label: str
    status: str
    assigned_chain_id: uuid.UUID | None = None
    secret: str

    @classmethod
    def from_issued(cls, issued: IssuedToken) -> "IssuedTokenResponse":
        return cls(
            token_id=issued.token_id,
            id_prefix=issued.id_prefix,
            label=issued.label,
            status=issued.status,
            assigned_chain_id=issued.assigned_chain_id,
            secret=issued.secret,
        )


class TokenResponse(BaseModel):
    """A secret-free view of a Gozar API key for listing (Requirement 8.3)."""

    token_id: uuid.UUID
    id_prefix: str
    label: str
    status: str
    assigned_chain_id: uuid.UUID | None = None
    assigned_chain_name: str | None = None
    limit: UsageLimitSpec | None = None
    usage: float = 0.0
    can_reveal: bool = False

    @classmethod
    def from_view(cls, view: TokenView) -> "TokenResponse":
        return cls(
            token_id=view.token_id,
            id_prefix=view.id_prefix,
            label=view.label,
            status=view.status,
            assigned_chain_id=view.assigned_chain_id,
            assigned_chain_name=view.assigned_chain_name,
            limit=view.limit,
            usage=view.usage,
            can_reveal=view.can_reveal,
        )


class TestTokenRouteRequest(BaseModel):
    """Small admin-console request executed through an existing Gozar API key."""

    model: str = Field(..., min_length=1, max_length=256)
    prompt: str = Field(..., min_length=1, max_length=20_000)
    chain_id: uuid.UUID | None = Field(
        default=None,
        description="Optional per-call chain override; otherwise the API key default applies.",
    )


# ---------------------------------------------------------------------------
# Fallback chains (Flow_Controller) -- Requirements 10.1, 10.4
# ---------------------------------------------------------------------------


class ChainEntryRequest(BaseModel):
    """One provider-aware node in a fallback chain."""

    account_id: uuid.UUID
    model: str | None = Field(
        default=None,
        max_length=256,
        description="Provider model override; null forwards the inbound request model.",
    )
    fallback_policy: FallbackPolicy = Field(
        default=FallbackPolicy.ANY_ERROR,
        description="When a failed attempt may continue to the next chain node.",
    )
    route: RouteKind = Field(
        default=RouteKind.CHAT,
        description="Request lane served by this node: chat or embeddings.",
    )


class CreateChainRequest(BaseModel):
    """Create a Fallback_Chain with ordered provider/model nodes."""

    name: str = Field(..., description="Operator-facing chain name.")
    entries: list[ChainEntryRequest] | None = Field(
        default=None,
        description="Provider-aware nodes in attempt order (position 0 first).",
    )
    account_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Legacy UUID-only nodes; prefer entries for new integrations.",
    )
    model_selector: str | None = Field(
        default=None, description="Optional model this chain serves."
    )

    @model_validator(mode="after")
    def validate_entry_shape(self) -> "CreateChainRequest":
        if self.entries is not None and self.account_ids is not None:
            raise ValueError("send either entries or account_ids, not both")
        return self

    @property
    def resolved_entries(self) -> list[ChainEntryRequest]:
        if self.entries is not None:
            return self.entries
        return [ChainEntryRequest(account_id=account_id) for account_id in self.account_ids or []]


class EditChainRequest(BaseModel):
    """Edit a chain. Only the fields explicitly supplied are changed.

    An explicit ``model_selector: null`` clears the selector, while omitting the
    field leaves it unchanged (the router uses ``model_fields_set`` to tell them
    apart).
    """

    name: str | None = None
    entries: list[ChainEntryRequest] | None = None
    account_ids: list[uuid.UUID] | None = None
    model_selector: str | None = None

    @model_validator(mode="after")
    def validate_entry_shape(self) -> "EditChainRequest":
        if self.entries is not None and self.account_ids is not None:
            raise ValueError("send either entries or account_ids, not both")
        return self

    @property
    def resolved_entries(self) -> list[ChainEntryRequest] | None:
        if self.entries is not None:
            return self.entries
        if self.account_ids is not None:
            return [ChainEntryRequest(account_id=account_id) for account_id in self.account_ids]
        return None


class UpsertChainRequest(CreateChainRequest):
    """Definition used to create or replace a chain at a stable client key."""


class ChainEntryResponse(BaseModel):
    """A single ordered entry of a chain."""

    account_id: uuid.UUID
    position: int
    model: str | None = None
    fallback_policy: FallbackPolicy = FallbackPolicy.ANY_ERROR
    route: RouteKind = RouteKind.CHAT


class ChainResponse(BaseModel):
    """A read view of a persisted Fallback_Chain."""

    chain_id: uuid.UUID
    name: str
    client_key: str | None = None
    model_selector: str | None = None
    entries: list[ChainEntryResponse]

    @classmethod
    def from_view(cls, view: ChainView) -> "ChainResponse":
        return cls(
            chain_id=view.chain_id,
            name=view.name,
            client_key=view.client_key,
            model_selector=view.model_selector,
            entries=[
                ChainEntryResponse(
                    account_id=e.account_id,
                    position=e.position,
                    model=e.model_id,
                    fallback_policy=e.fallback_policy,
                    route=e.route_kind,
                )
                for e in view.entries
            ],
        )


# ---------------------------------------------------------------------------
# Model catalog (Gateway catalog admin view)
# ---------------------------------------------------------------------------


class ModelCatalogAccountResponse(BaseModel):
    """Route-specific models reachable through one connected account."""

    account_id: uuid.UUID
    label: str
    provider: str
    kind: str
    status: str
    model_count: int
    models: list[OpenAIModelCard]
    embedding_model_count: int = 0
    embedding_models: list[OpenAIModelCard] = Field(default_factory=list)


class ChainIssueResponse(BaseModel):
    """Actionable reason a saved chain needs operator attention."""

    code: str
    message: str
    position: int | None = None
    account_id: uuid.UUID | None = None
    model: str | None = None
    route: RouteKind | None = None


class ModelCatalogChainResponse(BaseModel):
    """Models reachable through one saved fallback chain."""

    chain_id: uuid.UUID
    name: str
    model_selector: str | None = None
    entry_count: int
    chat_entry_count: int = 0
    embedding_entry_count: int = 0
    model_count: int
    models: list[OpenAIModelCard]
    embedding_model_count: int = 0
    embedding_models: list[OpenAIModelCard] = Field(default_factory=list)
    health: str = "healthy"
    issues: list[ChainIssueResponse] = Field(default_factory=list)


class ProviderModelCatalogResponse(BaseModel):
    """Editable fallback model list for one provider."""

    provider: str
    source: str
    model_count: int
    models: list[str]
    updated_at: datetime | None = None

    @classmethod
    def from_view(cls, view: ProviderModelCatalogView) -> "ProviderModelCatalogResponse":
        return cls(
            provider=view.provider,
            source=view.source,
            model_count=view.model_count,
            models=view.models,
            updated_at=view.updated_at,
        )


class UpdateProviderModelsRequest(BaseModel):
    """Replace the runtime fallback model list for a provider."""

    models: list[str] = Field(
        default_factory=list,
        description="Provider model ids, de-duplicated and persisted in order.",
    )


class ModelCatalogResponse(BaseModel):
    """Admin-facing model catalog grouped for the console UI."""

    generated_at: datetime
    cache_ttl_seconds: int
    refreshed: bool
    model_count: int
    models: list[OpenAIModelCard]
    embedding_model_count: int = 0
    embedding_models: list[OpenAIModelCard] = Field(default_factory=list)
    accounts: list[ModelCatalogAccountResponse]
    chains: list[ModelCatalogChainResponse]
    providers: list[ProviderModelCatalogResponse]
    unhealthy_chain_count: int = 0


# ---------------------------------------------------------------------------
# Traces (Usage_Recorder) -- Requirement 14.3
# ---------------------------------------------------------------------------


class TraceCredentialResponse(BaseModel):
    """Non-secret selected-credential snapshot carried on trace responses."""

    account_id: uuid.UUID
    label: str
    provider: str
    kind: str
    status: str


def _trace_credential(trace: TraceLog) -> TraceCredentialResponse | None:
    """Extract a non-secret credential snapshot from outbound metadata, if present."""
    meta = trace.outbound_meta or {}
    raw = meta.get("selected_credential")
    if not isinstance(raw, dict):
        return None
    try:
        account_id = uuid.UUID(str(raw.get("account_id")))
    except (TypeError, ValueError, AttributeError):
        return None
    label = raw.get("label")
    provider = raw.get("provider")
    kind = raw.get("kind")
    status = raw.get("status")
    if not all(isinstance(value, str) for value in (label, provider, kind, status)):
        return None
    return TraceCredentialResponse(
        account_id=account_id,
        label=label,
        provider=provider,
        kind=kind,
        status=status,
    )


class TraceSummaryResponse(BaseModel):
    """A trace list row: inbound shape, selected credential, outcome, duration."""

    correlation_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None = None
    outcome: str | None = None
    status_code: int | None = None
    account_id: uuid.UUID | None = None
    credential: TraceCredentialResponse | None = None
    elapsed_seconds: float | None = None

    @classmethod
    def from_trace(cls, trace: TraceLog) -> "TraceSummaryResponse":
        elapsed = trace_elapsed(trace)
        return cls(
            correlation_id=trace.correlation_id,
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            outcome=trace.outcome,
            status_code=trace.status_code,
            account_id=trace.account_id,
            credential=_trace_credential(trace),
            elapsed_seconds=elapsed.total_seconds() if elapsed is not None else None,
        )


class TraceDetailResponse(TraceSummaryResponse):
    """A single trace with its full inbound/outbound metadata (Requirement 14.3)."""

    inbound_meta: dict
    outbound_meta: dict | None = None

    @classmethod
    def from_trace(cls, trace: TraceLog) -> "TraceDetailResponse":
        elapsed = trace_elapsed(trace)
        return cls(
            correlation_id=trace.correlation_id,
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            outcome=trace.outcome,
            status_code=trace.status_code,
            account_id=trace.account_id,
            credential=_trace_credential(trace),
            elapsed_seconds=elapsed.total_seconds() if elapsed is not None else None,
            inbound_meta=trace.inbound_meta or {},
            outbound_meta=trace.outbound_meta,
        )


# ---------------------------------------------------------------------------
# Analytics (Analytics_Service) -- Requirements 15.1, 15.2, 15.3
# ---------------------------------------------------------------------------


class TimeRangeResponse(BaseModel):
    """The half-open ``[start, end)`` range a report covers."""

    start: datetime
    end: datetime

    @classmethod
    def from_range(cls, value: TimeRange) -> "TimeRangeResponse":
        return cls(start=value.start, end=value.end)


class TokenCountsResponse(BaseModel):
    """Aggregated request and token counts over a range."""

    request_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def from_counts(cls, counts: TokenCounts) -> "TokenCountsResponse":
        return cls(
            request_count=counts.request_count,
            prompt_tokens=counts.prompt_tokens,
            completion_tokens=counts.completion_tokens,
            total_tokens=counts.total_tokens,
        )


class LimitConsumptionResponse(BaseModel):
    """A subject's consumption over the range against its configured limit."""

    spec: UsageLimitSpec | None = None
    consumed: float | None = None
    percent_of_limit: float | None = None
    reached: bool | None = None

    @classmethod
    def from_consumption(
        cls, value: LimitConsumption
    ) -> "LimitConsumptionResponse":
        return cls(
            spec=value.spec,
            consumed=value.consumed,
            percent_of_limit=value.percent_of_limit,
            reached=value.reached,
        )


class TokenAnalyticsResponse(BaseModel):
    """Per-token report (Requirement 15.1)."""

    token_id: uuid.UUID
    range: TimeRangeResponse
    counts: TokenCountsResponse
    consumption: LimitConsumptionResponse

    @classmethod
    def from_report(cls, report: TokenAnalytics) -> "TokenAnalyticsResponse":
        return cls(
            token_id=report.token_id,
            range=TimeRangeResponse.from_range(report.range),
            counts=TokenCountsResponse.from_counts(report.counts),
            consumption=LimitConsumptionResponse.from_consumption(report.consumption),
        )


class AccountAnalyticsResponse(BaseModel):
    """Per-account report (Requirement 15.2)."""

    account_id: uuid.UUID
    range: TimeRangeResponse
    counts: TokenCountsResponse
    error_count: int
    consumption: LimitConsumptionResponse

    @classmethod
    def from_report(cls, report: AccountAnalytics) -> "AccountAnalyticsResponse":
        return cls(
            account_id=report.account_id,
            range=TimeRangeResponse.from_range(report.range),
            counts=TokenCountsResponse.from_counts(report.counts),
            error_count=report.error_count,
            consumption=LimitConsumptionResponse.from_consumption(report.consumption),
        )


class SystemAnalyticsResponse(BaseModel):
    """System-wide report (Requirement 15.3)."""

    range: TimeRangeResponse
    request_count: int
    error_count: int
    error_rate: float
    total_tokens: int

    @classmethod
    def from_report(cls, report: SystemAnalytics) -> "SystemAnalyticsResponse":
        return cls(
            range=TimeRangeResponse.from_range(report.range),
            request_count=report.request_count,
            error_count=report.error_count,
            error_rate=report.error_rate,
            total_tokens=report.total_tokens,
        )


__all__ = [
    "ApiKeyConnectRequest",
    "SubscriptionBeginRequest",
    "SubscriptionCompleteRequest",
    "SetEnabledRequest",
    "AuthorizationChallengeResponse",
    "AccountResponse",
    "CredentialSummaryResponse",
    "CreateTokenRequest",
    "RevealTokenRequest",
    "RotateTokenRequest",
    "IssuedTokenResponse",
    "TokenResponse",
    "TestTokenRouteRequest",
    "ChainEntryRequest",
    "CreateChainRequest",
    "EditChainRequest",
    "UpsertChainRequest",
    "ChainEntryResponse",
    "ChainResponse",
    "ModelCatalogAccountResponse",
    "ModelCatalogChainResponse",
    "ChainIssueResponse",
    "ModelCatalogResponse",
    "TraceSummaryResponse",
    "TraceDetailResponse",
    "TimeRangeResponse",
    "TokenCountsResponse",
    "LimitConsumptionResponse",
    "TokenAnalyticsResponse",
    "AccountAnalyticsResponse",
    "SystemAnalyticsResponse",
]
