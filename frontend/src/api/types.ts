/**
 * TypeScript interfaces for the Gozar admin API request/response bodies.
 *
 * These mirror the backend Pydantic models in `gozar/api/schemas.py` and the
 * Auth_Service value objects in `gozar/auth/session.py`. They are the single
 * source of truth for API shapes on the client: no `any` is used at an API
 * boundary (steering section 8). Field names match the JSON exactly (snake_case),
 * since FastAPI serializes the Pydantic field names verbatim.
 *
 * Timestamps are ISO-8601 strings (FastAPI renders `datetime` as ISO strings).
 * UUIDs are plain strings.
 */

// ---------------------------------------------------------------------------
// Shared error envelope (gozar/core/errors.py admin envelope)
// ---------------------------------------------------------------------------

/** A single structured detail entry inside an error envelope. */
export interface ApiErrorDetail {
  readonly field?: string;
  readonly message?: string;
  readonly [key: string]: unknown;
}

/** The admin error envelope: `{ "error": { code, message, details } }`. */
export interface ApiErrorEnvelope {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly details: ReadonlyArray<ApiErrorDetail>;
  };
}

// ---------------------------------------------------------------------------
// Usage limits (gozar/usage/limits.py)
// ---------------------------------------------------------------------------

export type LimitMetric =
  | "request_count"
  | "token_count"
  | "cost_estimate"
  | "percentage";

export type LimitWindow = "none" | "daily" | "monthly" | "rolling_24h";

/** A single Usage_Limit specification attached to a credential or token. */
export interface UsageLimitSpec {
  readonly metric: LimitMetric;
  readonly limit_value: number;
  readonly capacity?: number | null;
  readonly window: LimitWindow;
}

// ---------------------------------------------------------------------------
// Auth (gozar/auth/session.py SessionTokens; expected /api/auth surface)
// ---------------------------------------------------------------------------

/** Login/refresh request body. */
export interface LoginRequest {
  readonly username: string;
  readonly password: string;
}

export interface RefreshRequest {
  readonly refresh_token: string;
}

/** The OAuth2-style token bundle returned on successful login or refresh. */
export interface SessionTokens {
  readonly access_token: string;
  readonly refresh_token: string;
  readonly token_type: string;
  readonly expires_in: number;
}

/** First-run bootstrap gate (gozar/auth/service.py bootstrap_required). */
export interface BootstrapStatus {
  readonly bootstrap_required: boolean;
}

/**
 * Request body for creating the first administrator during first-run bootstrap
 * (POST /api/auth/bootstrap). The backend applies its password policy (min 12
 * chars, mixed case, digit, symbol) and returns a {@link SessionTokens} bundle on
 * success, exactly like login.
 */
export interface BootstrapCreateRequest {
  readonly username: string;
  readonly password: string;
}

// ---------------------------------------------------------------------------
// Accounts (Account_Manager) -- AccountResponse, CredentialSummaryResponse, ...
// ---------------------------------------------------------------------------

export type CredentialKind = "subscription" | "api_key";

export type AccountStatus = "active" | "disabled" | "requires_reauth";

/** How Gozar authenticates to a provider (mirrors `gozar/providers/registry.py`). */
export type AuthStyle = "subscription_oauth" | "api_key";

export interface AccountResponse {
  readonly account_id: string;
  readonly provider: string;
  readonly kind: string;
  readonly label: string;
  readonly status: string;
  readonly connected_at: string;
  readonly limit?: UsageLimitSpec | null;
  readonly consumption: number;
}

export interface CredentialSummaryResponse {
  readonly account_id: string;
  readonly provider: string;
  readonly kind: string;
  readonly label: string;
  readonly status: string;
}

export interface ApiKeyConnectRequest {
  readonly provider: string;
  readonly api_key: string;
  readonly label?: string | null;
}

export interface SubscriptionBeginRequest {
  readonly provider: string;
}

export type SubscriptionDeviceBeginRequest = SubscriptionBeginRequest;

export interface SubscriptionDeviceCompleteRequest {
  readonly pending_id: string;
  readonly label?: string | null;
}

export interface DeviceAuthorizationChallengeResponse {
  readonly pending_id: string;
  readonly verification_url: string;
  readonly user_code: string;
  readonly interval_seconds: number;
}

export interface DeviceAuthorizationCompleteResponse {
  readonly status: "pending" | "connected";
  readonly account?: CredentialSummaryResponse | null;
}

export interface SubscriptionCompleteRequest {
  readonly pending_id: string;
  /**
   * Either a bare authorization code OR the full redirect URL pasted from the
   * browser's address bar; the backend extracts the code (and state) from it.
   */
  readonly code: string;
  /** Optional anti-CSRF state; may be omitted (the backend recovers it from a pasted URL). */
  readonly state?: string | null;
  readonly label?: string | null;
}

export interface AuthorizationChallengeResponse {
  readonly pending_id: string;
  readonly authorize_url: string;
  readonly state: string;
}

export interface SetEnabledRequest {
  readonly enabled: boolean;
}

// ---------------------------------------------------------------------------
// Tokens (Token_Authority) -- IssuedTokenResponse, TokenResponse
// ---------------------------------------------------------------------------

export interface CreateTokenRequest {
  readonly label: string;
  readonly limit?: UsageLimitSpec | null;
  readonly assigned_chain_id?: string | null;
}

export interface SetTokenChainRequest {
  readonly assigned_chain_id?: string | null;
}

export interface RotateTokenRequest {
  readonly password: string;
}

export interface RevealTokenRequest {
  readonly password: string;
  readonly existing_api_key?: string | null;
}

/** Secret-bearing response returned by create, reveal, or explicit rotation. */
export interface IssuedTokenResponse {
  readonly token_id: string;
  readonly id_prefix: string;
  readonly label: string;
  readonly status: string;
  readonly assigned_chain_id?: string | null;
  readonly secret: string;
}

export interface TokenResponse {
  readonly token_id: string;
  readonly id_prefix: string;
  readonly label: string;
  readonly status: string;
  readonly assigned_chain_id?: string | null;
  readonly assigned_chain_name?: string | null;
  readonly limit?: UsageLimitSpec | null;
  readonly usage: number;
  readonly can_reveal?: boolean;
}

export interface ModelCardResponse {
  readonly id: string;
  readonly object: "model";
  readonly created?: number | null;
  readonly owned_by?: string | null;
}

export interface ModelListResponse {
  readonly object: "list";
  readonly data: ReadonlyArray<ModelCardResponse>;
}

export interface ModelCatalogAccountResponse {
  readonly account_id: string;
  readonly label: string;
  readonly provider: string;
  readonly kind: string;
  readonly status: string;
  readonly model_count: number;
  readonly models: ReadonlyArray<ModelCardResponse>;
}

export interface ModelCatalogChainResponse {
  readonly chain_id: string;
  readonly name: string;
  readonly model_selector?: string | null;
  readonly entry_count: number;
  readonly model_count: number;
  readonly models: ReadonlyArray<ModelCardResponse>;
  readonly health: "healthy" | "warning" | "broken" | string;
  readonly issues: ReadonlyArray<ChainIssueResponse>;
}

export interface ChainIssueResponse {
  readonly code: string;
  readonly message: string;
  readonly position?: number | null;
  readonly account_id?: string | null;
  readonly model?: string | null;
}

export interface ProviderModelCatalogResponse {
  readonly provider: string;
  readonly source: "environment" | "runtime" | string;
  readonly model_count: number;
  readonly models: ReadonlyArray<string>;
  readonly updated_at?: string | null;
}

export interface UpdateProviderModelsRequest {
  readonly models: ReadonlyArray<string>;
}

export interface ModelCatalogResponse {
  readonly generated_at: string;
  readonly cache_ttl_seconds: number;
  readonly refreshed: boolean;
  readonly model_count: number;
  readonly models: ReadonlyArray<ModelCardResponse>;
  readonly accounts: ReadonlyArray<ModelCatalogAccountResponse>;
  readonly chains: ReadonlyArray<ModelCatalogChainResponse>;
  readonly providers: ReadonlyArray<ProviderModelCatalogResponse>;
  readonly unhealthy_chain_count: number;
}

// ---------------------------------------------------------------------------
// Fallback chains (Flow_Controller) -- ChainResponse, Create/EditChainRequest
// ---------------------------------------------------------------------------

export interface ChainEntryResponse {
  readonly account_id: string;
  readonly position: number;
  readonly model?: string | null;
  readonly fallback_policy?: FallbackPolicy;
}

export type FallbackPolicy = "any_error" | "auth_or_retryable" | "retryable";

export interface ChainEntryRequest {
  readonly account_id: string;
  readonly model?: string | null;
  readonly fallback_policy?: FallbackPolicy;
}

export interface ChainResponse {
  readonly chain_id: string;
  readonly name: string;
  readonly client_key?: string | null;
  readonly model_selector?: string | null;
  readonly entries: ReadonlyArray<ChainEntryResponse>;
}

export interface CreateChainRequest {
  readonly name: string;
  readonly entries: ReadonlyArray<ChainEntryRequest>;
  readonly model_selector?: string | null;
}

export interface EditChainRequest {
  readonly name?: string;
  readonly entries?: ReadonlyArray<ChainEntryRequest>;
  readonly model_selector?: string | null;
}

export interface TestTokenRouteRequest {
  readonly model: string;
  readonly prompt: string;
  readonly chain_id?: string | null;
}

export interface ChatCompletionMessageResponse {
  readonly role: string;
  readonly content?: unknown;
}

export interface ChatCompletionChoiceResponse {
  readonly index: number;
  readonly message: ChatCompletionMessageResponse;
  readonly finish_reason?: string | null;
}

export interface ChatCompletionResponse {
  readonly id: string;
  readonly object: string;
  readonly created: number;
  readonly model: string;
  readonly choices: ReadonlyArray<ChatCompletionChoiceResponse>;
  readonly usage?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Traces (Usage_Recorder) -- TraceSummaryResponse, TraceDetailResponse
// ---------------------------------------------------------------------------

export interface TraceCredentialResponse {
  readonly account_id: string;
  readonly label: string;
  readonly provider: string;
  readonly kind: string;
  readonly status: string;
}

export interface TraceSummaryResponse {
  readonly correlation_id: string;
  readonly started_at: string;
  readonly ended_at?: string | null;
  readonly outcome?: string | null;
  readonly status_code?: number | null;
  readonly account_id?: string | null;
  readonly credential?: TraceCredentialResponse | null;
  readonly elapsed_seconds?: number | null;
}

export interface TraceDetailResponse extends TraceSummaryResponse {
  readonly inbound_meta: Record<string, unknown>;
  readonly outbound_meta?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Analytics (Analytics_Service)
// ---------------------------------------------------------------------------

export interface TimeRangeResponse {
  readonly start: string;
  readonly end: string;
}

export interface TokenCountsResponse {
  readonly request_count: number;
  readonly prompt_tokens: number;
  readonly completion_tokens: number;
  readonly total_tokens: number;
}

export interface LimitConsumptionResponse {
  readonly spec?: UsageLimitSpec | null;
  readonly consumed?: number | null;
  readonly percent_of_limit?: number | null;
  readonly reached?: boolean | null;
}

export interface TokenAnalyticsResponse {
  readonly token_id: string;
  readonly range: TimeRangeResponse;
  readonly counts: TokenCountsResponse;
  readonly consumption: LimitConsumptionResponse;
}

export interface AccountAnalyticsResponse {
  readonly account_id: string;
  readonly range: TimeRangeResponse;
  readonly counts: TokenCountsResponse;
  readonly error_count: number;
  readonly consumption: LimitConsumptionResponse;
}

export interface SystemAnalyticsResponse {
  readonly range: TimeRangeResponse;
  readonly request_count: number;
  readonly error_count: number;
  readonly error_rate: number;
  readonly total_tokens: number;
}
