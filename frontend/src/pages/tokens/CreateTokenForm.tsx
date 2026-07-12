import { useState, type FormEvent } from "react";

import { createToken } from "../../api/tokens";
import { ApiError } from "../../api/errors";
import { AlertIcon } from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type { ChainResponse, IssuedTokenResponse } from "../../api/types";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

/**
 * Create a new Gozar API key (Requirement 8.1). Collects an operator-facing label
 * and issues the key; the usage limit is configured afterwards from the row
 * action (mirroring how accounts connect first, then set a limit).
 *
 * Owns its own loading and error states. On success it hands the freshly issued
 * token -- the only payload that carries the secret -- to {@link onCreated} so the
 * parent can present the returned secret and refresh the list.
 */
export function CreateTokenForm({
  chains,
  onCreated,
}: {
  readonly chains: ReadonlyArray<ChainResponse>;
  readonly onCreated: (issued: IssuedTokenResponse) => void;
}): JSX.Element {
  const [label, setLabel] = useState("");
  const [assignedChainId, setAssignedChainId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedChain =
    assignedChainId === ""
      ? null
      : chains.find((chain) => chain.chain_id === assignedChainId) ?? null;

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const issued = await createToken({
        label: label.trim(),
        limit: null,
        assigned_chain_id: assignedChainId === "" ? null : assignedChainId,
      });
      onCreated(issued);
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="form" onSubmit={handleSubmit} noValidate>
      <p className="form__hint">
        Create one API key per application or workflow. The selected route is carried
        by the key, so app requests do not need custom chain headers.
      </p>

      <div className="form-grid">
        <div className="field">
          <label htmlFor="token-label">Label</label>
          <input
            id="token-label"
            type="text"
            autoComplete="off"
            placeholder="Production API, LangGraph worker, CI pipeline"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            disabled={submitting}
            required
          />
          <p className="field__hint">Shown only in the operator console.</p>
        </div>

        <div className="field">
          <label htmlFor="token-chain">Routing chain</label>
          <select
            id="token-chain"
            value={assignedChainId}
            onChange={(e) => setAssignedChainId(e.target.value)}
            disabled={submitting}
          >
            <option value="">Auto-select a chain</option>
            {chains.map((chain) => (
              <option key={chain.chain_id} value={chain.chain_id}>
                {chain.name}
                {chain.model_selector ? ` (${chain.model_selector})` : " (catch-all)"}
              </option>
            ))}
          </select>
          <p className="field__hint">
            Pin a chain when this API key must always use one route.
          </p>
        </div>
      </div>

      <div className="routing-summary" aria-live="polite">
        <span className="routing-summary__label">Request routing</span>
        {selectedChain === null ? (
          <p>
            This API key uses Gozar's default chain selection for each request.
          </p>
        ) : (
          <p>
            This API key is pinned to <strong>{selectedChain.name}</strong> with{" "}
            {selectedChain.entries.length} ordered{" "}
            {selectedChain.entries.length === 1 ? "entry" : "entries"}.
          </p>
        )}
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
          disabled={submitting || label.trim().length === 0}
        >
          {submitting ? (
            <>
              <Spinner label="Creating" size={18} />
              <span>Creating...</span>
            </>
          ) : (
              <span>Create API key</span>
          )}
        </button>
      </div>
    </form>
  );
}
