import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  beginSubscriptionDeviceConnect,
  beginSubscriptionConnect,
  completeSubscriptionDeviceConnect,
  completeSubscriptionConnect,
  connectApiKey,
} from "../../api/accounts";
import { ApiError } from "../../api/errors";
import { AlertIcon, CheckIcon, CopyIcon, ExternalLinkIcon, KeyIcon, LinkIcon } from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type {
  AuthorizationChallengeResponse,
  DeviceAuthorizationChallengeResponse,
} from "../../api/types";
import { API_KEY_PROVIDERS, SUBSCRIPTION_PROVIDERS } from "./providers";

type Method = "api_key" | "subscription";
type SubscriptionFlow = "device" | "redirect";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

/**
 * Connect a new upstream account. Offers two methods:
 *  - API key: validate and store a metered key in one step.
 *  - Subscription: begin an OAuth + PKCE flow (the verifier stays server-side),
 *    open the provider authorize URL, then complete with the returned code/state.
 *
 * Owns its own loading and error states; calls {@link onConnected} after a
 * successful connect so the parent can refresh the list and close the dialog.
 */
export function ConnectForm({
  onConnected,
}: {
  readonly onConnected: () => void;
}): JSX.Element {
  const [method, setMethod] = useState<Method>("api_key");

  return (
    <div className="connect">
      <div
        className="segmented"
        role="tablist"
        aria-label="Connection method"
      >
        <button
          type="button"
          role="tab"
          aria-selected={method === "api_key"}
          className={`segmented__option${method === "api_key" ? " segmented__option--active" : ""}`}
          onClick={() => setMethod("api_key")}
        >
          <KeyIcon size={16} aria-hidden />
          <span>API key</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={method === "subscription"}
          className={`segmented__option${method === "subscription" ? " segmented__option--active" : ""}`}
          onClick={() => setMethod("subscription")}
        >
          <LinkIcon size={16} aria-hidden />
          <span>Subscription</span>
        </button>
      </div>

      {method === "api_key" ? (
        <ApiKeyConnect onConnected={onConnected} />
      ) : (
        <SubscriptionConnect onConnected={onConnected} />
      )}
    </div>
  );
}

function ApiKeyConnect({ onConnected }: { readonly onConnected: () => void }): JSX.Element {
  const firstProvider = API_KEY_PROVIDERS[0]?.id ?? "";
  const [provider, setProvider] = useState<string>(firstProvider);
  const [apiKey, setApiKey] = useState("");
  const [label, setLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await connectApiKey({
        provider,
        api_key: apiKey,
        label: label.trim() === "" ? null : label.trim(),
      });
      onConnected();
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="form" onSubmit={handleSubmit} noValidate>
      <div className="field">
        <label htmlFor="apikey-provider">Provider</label>
        <select
          id="apikey-provider"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          disabled={submitting}
        >
          {API_KEY_PROVIDERS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="apikey-secret">API key</label>
        <input
          id="apikey-secret"
          type="password"
          autoComplete="off"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          disabled={submitting}
          required
        />
      </div>

      <div className="field">
        <label htmlFor="apikey-label">Label (optional)</label>
        <input
          id="apikey-label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          disabled={submitting}
        />
      </div>

      {error !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{error}</span>
        </p>
      )}

      <div className="form__actions">
        <button
          type="submit"
          className="button button--primary"
          disabled={submitting || provider === "" || apiKey.length === 0}
        >
          {submitting ? (
            <>
              <Spinner label="Connecting" size={18} />
              <span>Connecting...</span>
            </>
          ) : (
            <span>Connect</span>
          )}
        </button>
      </div>
    </form>
  );
}

function SubscriptionConnect({
  onConnected,
}: {
  readonly onConnected: () => void;
}): JSX.Element {
  const firstProvider = SUBSCRIPTION_PROVIDERS[0]?.id ?? "";
  const [provider, setProvider] = useState<string>(firstProvider);
  const [label, setLabel] = useState("");
  const [flow, setFlow] = useState<SubscriptionFlow | null>(null);
  const [challenge, setChallenge] = useState<AuthorizationChallengeResponse | null>(null);
  const [deviceChallenge, setDeviceChallenge] =
    useState<DeviceAuthorizationChallengeResponse | null>(null);
  const [pasted, setPasted] = useState("");
  const [copied, setCopied] = useState(false);
  const [codeCopied, setCodeCopied] = useState(false);
  const [deviceStatus, setDeviceStatus] = useState<"idle" | "waiting" | "checking">("idle");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollingRef = useRef(false);
  const supportsDeviceCode = provider === "codex";
  const hasProvider = provider.length > 0;

  function resetConnectState(): void {
    setFlow(null);
    setChallenge(null);
    setDeviceChallenge(null);
    setPasted("");
    setCopied(false);
    setCodeCopied(false);
    setDeviceStatus("idle");
    setError(null);
  }

  async function copyText(value: string, onCopied: (copied: boolean) => void): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      onCopied(true);
    } catch {
      setError("Could not copy automatically. Select the value and copy it manually.");
    }
  }

  async function handleBeginDevice(event?: FormEvent<HTMLFormElement>): Promise<void> {
    event?.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await beginSubscriptionDeviceConnect({ provider });
      setDeviceChallenge(result);
      setFlow("device");
      setDeviceStatus("waiting");
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleBeginRedirect(): Promise<void> {
    setError(null);
    setSubmitting(true);
    try {
      const result = await beginSubscriptionConnect({ provider });
      setChallenge(result);
      setFlow("redirect");
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCopyUrl(): Promise<void> {
    if (challenge === null) {
      return;
    }
    await copyText(challenge.authorize_url, setCopied);
  }

  async function handleCopyCode(): Promise<void> {
    if (deviceChallenge === null) {
      return;
    }
    await copyText(deviceChallenge.user_code, setCodeCopied);
  }

  async function pollDeviceChallenge(): Promise<void> {
    if (deviceChallenge === null || pollingRef.current) {
      return;
    }
    pollingRef.current = true;
    setDeviceStatus("checking");
    setError(null);
    try {
      const result = await completeSubscriptionDeviceConnect({
        pending_id: deviceChallenge.pending_id,
        label: label.trim() === "" ? null : label.trim(),
      });
      if (result.status === "connected") {
        onConnected();
        return;
      }
      setDeviceStatus("waiting");
    } catch (cause) {
      setError(messageFor(cause));
      setDeviceStatus("waiting");
    } finally {
      pollingRef.current = false;
    }
  }

  async function handleComplete(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (challenge === null) {
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      // The operator pastes either the full localhost redirect URL or just the code.
      // The backend extracts the code (and state) from it, so state is omitted here.
      await completeSubscriptionConnect({
        pending_id: challenge.pending_id,
        code: pasted.trim(),
        label: label.trim() === "" ? null : label.trim(),
      });
      onConnected();
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setSubmitting(false);
    }
  }

  useEffect(() => {
    if (flow !== "device" || deviceChallenge === null) {
      return undefined;
    }
    const interval = window.setInterval(
      () => {
        void pollDeviceChallenge();
      },
      Math.max(deviceChallenge.interval_seconds, 2) * 1000,
    );
    return () => window.clearInterval(interval);
  }, [flow, deviceChallenge?.pending_id]);

  if (flow === null) {
    return (
      <form
        className="form"
        onSubmit={supportsDeviceCode ? handleBeginDevice : (event) => {
          event.preventDefault();
          void handleBeginRedirect();
        }}
        noValidate
      >
        <div className="field">
          <label htmlFor="sub-provider">Provider</label>
          <select
            id="sub-provider"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              resetConnectState();
            }}
            disabled={submitting}
          >
            {SUBSCRIPTION_PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="sub-label">Label (optional)</label>
          <input
            id="sub-label"
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            disabled={submitting}
          />
        </div>

        <p className="form__hint">
          {supportsDeviceCode
            ? "Codex uses device-code sign-in by default, so no localhost callback or copied redirect URL is needed."
            : "This provider uses the browser redirect flow. Paste the returned code or callback URL after approval."}
        </p>

        {error !== null && (
          <p className="alert alert--error" role="alert">
            <AlertIcon size={18} aria-hidden />
            <span>{error}</span>
          </p>
        )}

        <div className="form__actions">
          <button
            type="submit"
            className="button button--primary"
            disabled={submitting || !hasProvider}
          >
            {submitting ? (
              <>
                <Spinner label="Starting" size={18} />
                <span>Starting...</span>
              </>
            ) : (
              <span>{supportsDeviceCode ? "Start device sign-in" : "Start authorization"}</span>
            )}
          </button>
          {supportsDeviceCode && (
            <button
              type="button"
              className="button button--ghost"
              disabled={submitting || !hasProvider}
              onClick={() => void handleBeginRedirect()}
            >
              <span>Use redirect URL</span>
            </button>
          )}
        </div>
      </form>
    );
  }

  if (flow === "device" && deviceChallenge !== null) {
    return (
      <div className="form">
        <ol className="form__steps">
          <li>Open the verification page in any browser.</li>
          <li>Enter the one-time code shown below.</li>
          <li>Keep this dialog open; Gozar will finish as soon as OpenAI approves it.</li>
        </ol>

        <div className="field">
          <label htmlFor="sub-device-url">Verification page</label>
          <div className="secret-row">
            <input
              id="sub-device-url"
              type="text"
              className="secret-row__value"
              value={deviceChallenge.verification_url}
              readOnly
              spellCheck={false}
              onFocus={(e) => e.currentTarget.select()}
            />
            <a
              className="button button--ghost"
              href={deviceChallenge.verification_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLinkIcon size={18} aria-hidden />
              <span>Open</span>
            </a>
          </div>
        </div>

        <div className="field">
          <label htmlFor="sub-device-code">One-time code</label>
          <div className="secret-row">
            <input
              id="sub-device-code"
              type="text"
              className="secret-row__value secret-row__value--code"
              value={deviceChallenge.user_code}
              readOnly
              spellCheck={false}
              onFocus={(e) => e.currentTarget.select()}
            />
            <button
              type="button"
              className="button button--ghost"
              onClick={() => void handleCopyCode()}
              aria-label="Copy one-time code to clipboard"
            >
              {codeCopied ? (
                <>
                  <CheckIcon size={18} aria-hidden />
                  <span>Copied</span>
                </>
              ) : (
                <>
                  <CopyIcon size={18} aria-hidden />
                  <span>Copy</span>
                </>
              )}
            </button>
          </div>
        </div>

        <p className="alert alert--warn">
          <AlertIcon size={18} aria-hidden />
          <span>Continue only if you started this sign-in from Gozar.</span>
        </p>

        {error !== null && (
          <p className="alert alert--error" role="alert">
            <AlertIcon size={18} aria-hidden />
            <span>{error}</span>
          </p>
        )}

        <div className="form__actions">
          <button
            type="button"
            className="button button--ghost"
            onClick={resetConnectState}
            disabled={submitting}
          >
            <span>Back</span>
          </button>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => void handleBeginRedirect()}
            disabled={submitting}
          >
            <span>Use redirect URL</span>
          </button>
          <button
            type="button"
            className="button button--primary"
            onClick={() => void pollDeviceChallenge()}
            disabled={deviceStatus === "checking"}
          >
            {deviceStatus === "checking" ? (
              <>
                <Spinner label="Checking" size={18} />
                <span>Checking...</span>
              </>
            ) : (
              <span>Check status</span>
            )}
          </button>
        </div>
      </div>
    );
  }

  if (challenge === null) {
    return (
      <p className="alert alert--error" role="alert">
        <AlertIcon size={18} aria-hidden />
        <span>Authorization session is unavailable. Go back and start again.</span>
      </p>
    );
  }
  const redirectChallenge = challenge;

  return (
    <form className="form" onSubmit={handleComplete} noValidate>
      <ol className="form__steps">
        <li>Open the authorization URL below in your browser and sign in.</li>
        <li>
          You will be redirected to a <code>localhost</code> page that will not load.
          That is expected for the built-in Codex/CLI OAuth client.
        </li>
        <li>
          Copy the FULL address from your browser&apos;s address bar and paste it
          below (or just the <code>code</code> value).
        </li>
      </ol>

      <div className="field">
        <label htmlFor="sub-authorize-url">Authorization URL</label>
        <div className="secret-row">
          <input
            id="sub-authorize-url"
            type="text"
            className="secret-row__value"
            value={redirectChallenge.authorize_url}
            readOnly
            spellCheck={false}
            onFocus={(e) => e.currentTarget.select()}
          />
          <button
            type="button"
            className="button button--ghost"
            onClick={() => void handleCopyUrl()}
            aria-label="Copy authorization URL to clipboard"
          >
            {copied ? (
              <>
                <CheckIcon size={18} aria-hidden />
                <span>Copied</span>
              </>
            ) : (
              <>
                <CopyIcon size={18} aria-hidden />
                <span>Copy</span>
              </>
            )}
          </button>
        </div>
      </div>

      <a
        className="button button--ghost"
        href={redirectChallenge.authorize_url}
        target="_blank"
        rel="noopener noreferrer"
      >
        <ExternalLinkIcon size={18} aria-hidden />
        <span>Open in new tab</span>
      </a>

      <div className="field">
        <label htmlFor="sub-pasted">Paste the redirect URL (or code)</label>
        <input
          id="sub-pasted"
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder="http://localhost:1455/auth/callback?code=..."
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
          disabled={submitting}
          required
        />
      </div>

      {error !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{error}</span>
        </p>
      )}

      <div className="form__actions">
        <button
          type="button"
          className="button button--ghost"
          onClick={() => {
            resetConnectState();
          }}
          disabled={submitting}
        >
          <span>Back</span>
        </button>
        <button
          type="submit"
          className="button button--primary"
          disabled={submitting || pasted.trim().length === 0}
        >
          {submitting ? (
            <>
              <Spinner label="Completing" size={18} />
              <span>Completing...</span>
            </>
          ) : (
            <span>Finish connecting</span>
          )}
        </button>
      </div>
    </form>
  );
}
