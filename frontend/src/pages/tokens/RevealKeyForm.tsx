import { useState, type FormEvent } from "react";

import { AlertIcon, KeyIcon } from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type { TokenResponse } from "../../api/types";

export function RevealKeyForm({
  token,
  submitting,
  error,
  onSubmit,
}: {
  readonly token: TokenResponse;
  readonly submitting: boolean;
  readonly error: string | null;
  readonly onSubmit: (password: string, existingApiKey?: string) => void;
}): JSX.Element {
  const [password, setPassword] = useState("");
  const [existingApiKey, setExistingApiKey] = useState("");
  const legacyReveal = token.can_reveal === false;
  const submitDisabled =
    submitting ||
    password.length === 0 ||
    (legacyReveal && existingApiKey.trim().length === 0);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    onSubmit(password, legacyReveal ? existingApiKey.trim() : undefined);
  }

  return (
    <form className="form" onSubmit={handleSubmit} noValidate>
      <input
        type="text"
        name="username"
        autoComplete="username"
        value="gozar-operator"
        readOnly
        hidden
      />
      <p className="alert alert--warn" role="alert">
        <KeyIcon size={18} aria-hidden />
        {legacyReveal ? (
          <span>
            This older API key was created before encrypted reveal storage. Paste
            the full existing key once; Gozar verifies it, stores an encrypted copy,
            and does not create a replacement.
          </span>
        ) : (
          <span>
            This reveals the existing API key for <strong>{token.label}</strong>.
            It does not create a replacement and does not revoke the current key.
          </span>
        )}
      </p>

      {legacyReveal && (
        <div className="field">
          <label htmlFor="legacy-existing-api-key">Existing API key</label>
          <input
            id="legacy-existing-api-key"
            name="existing-api-key"
            type="password"
            autoComplete="off"
            placeholder="gz-..."
            value={existingApiKey}
            onChange={(event) => setExistingApiKey(event.target.value)}
            disabled={submitting}
            required
          />
          <p className="field__hint">
            This must be the current key, not a new generated value.
          </p>
        </div>
      )}

      <div className="field">
        <label htmlFor="reveal-password">Your password</label>
        <input
          id="reveal-password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={submitting}
          required
        />
        <p className="field__hint">
          Only keys created with encrypted reveal support can be shown again.
        </p>
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
          disabled={submitDisabled}
        >
          {submitting ? (
            <>
              <Spinner label="Revealing" size={18} />
              <span>Revealing...</span>
            </>
          ) : (
            <span>{legacyReveal ? "Save and reveal key" : "Reveal key"}</span>
          )}
        </button>
      </div>
    </form>
  );
}
