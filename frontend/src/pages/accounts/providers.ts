import type { AuthStyle } from "../../api/types";

/**
 * Catalog of upstream providers the Account_Manager can connect, mirroring the
 * backend `ProviderId` / `AuthStyle` enums in `gozar/providers/registry.py`.
 *
 * These are intrinsic domain identifiers (not deployment- or locale-specific
 * values), so the connect UI can present the right flow per provider: API-key
 * providers use the metered-key form, subscription providers use OAuth + PKCE.
 * A deployment may not have every provider configured; an unconfigured provider
 * simply fails the connect call with a descriptive backend error.
 */
export interface ProviderOption {
  /** Provider id sent to the backend (matches the `ProviderId` enum value). */
  readonly id: string;
  /** Operator-facing display name. */
  readonly label: string;
  /** How Gozar authenticates to this provider. */
  readonly authStyle: AuthStyle;
  /** Whether this provider can serve the OpenAI-compatible Embeddings endpoint. */
  readonly supportsEmbeddings: boolean;
}

export const PROVIDERS: ReadonlyArray<ProviderOption> = [
  { id: "openai", label: "OpenAI", authStyle: "api_key", supportsEmbeddings: true },
  {
    id: "openrouter",
    label: "OpenRouter",
    authStyle: "api_key",
    supportsEmbeddings: true,
  },
  {
    id: "codex",
    label: "Codex (ChatGPT subscription)",
    authStyle: "subscription_oauth",
    supportsEmbeddings: false,
  },
  {
    id: "anthropic",
    label: "Anthropic (Claude subscription)",
    authStyle: "subscription_oauth",
    supportsEmbeddings: false,
  },
] as const;

/** Providers connected via a metered API key. */
export const API_KEY_PROVIDERS: ReadonlyArray<ProviderOption> = PROVIDERS.filter(
  (p) => p.authStyle === "api_key",
);

/** Providers connected via subscription OAuth + PKCE. */
export const SUBSCRIPTION_PROVIDERS: ReadonlyArray<ProviderOption> = PROVIDERS.filter(
  (p) => p.authStyle === "subscription_oauth",
);

/** Human-readable provider name, falling back to the raw id when unknown. */
export function providerLabel(id: string): string {
  return PROVIDERS.find((p) => p.id === id)?.label ?? id;
}

/** True when the provider can be used in an Embeddings chain lane. */
export function providerSupportsEmbeddings(id: string): boolean {
  return PROVIDERS.find((provider) => provider.id === id)?.supportsEmbeddings ?? false;
}
