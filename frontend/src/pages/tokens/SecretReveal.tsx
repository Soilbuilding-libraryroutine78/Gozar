import { useState } from "react";

import { AlertIcon, CheckIcon, CopyIcon } from "../../components/icons";
import type { IssuedTokenResponse } from "../../api/types";

/** Present a Gozar API key secret returned by create or password-confirmed reveal. */
export function SecretReveal({
  issued,
  onDone,
}: {
  readonly issued: IssuedTokenResponse;
  readonly onDone: () => void;
}): JSX.Element {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  async function handleCopy(): Promise<void> {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(issued.secret);
      setCopied(true);
    } catch {
      // Clipboard access can be blocked (insecure context, denied permission).
      setCopyError("Could not copy automatically. Select the value and copy it manually.");
    }
  }

  return (
    <div className="form">
      <p className="alert alert--warn" role="alert">
        <AlertIcon size={18} aria-hidden />
        <span>
          Copy this API key and store it securely. It can be revealed again only
          after operator password confirmation while the key is not revoked.
        </span>
      </p>

      <div className="field">
        <label htmlFor="issued-secret">Gozar API key</label>
        <div className="secret-row">
          <input
            id="issued-secret"
            type="text"
            className="secret-row__value"
            value={issued.secret}
            readOnly
            spellCheck={false}
            onFocus={(e) => e.currentTarget.select()}
          />
          <button
            type="button"
            className="button button--ghost"
            onClick={() => void handleCopy()}
            aria-label="Copy Gozar API key to clipboard"
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

      {copyError !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{copyError}</span>
        </p>
      )}

      <dl className="detail-list">
        <div className="detail-list__row">
          <dt>Label</dt>
          <dd>{issued.label}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Identifier prefix</dt>
          <dd>
            <code>{issued.id_prefix}</code>
          </dd>
        </div>
      </dl>

      <div className="form__actions">
        <button type="button" className="button button--primary" onClick={onDone}>
          <span>I have copied the key</span>
        </button>
      </div>
    </div>
  );
}
