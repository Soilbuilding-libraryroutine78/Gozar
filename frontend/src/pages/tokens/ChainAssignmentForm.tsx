import { useState, type FormEvent } from "react";

import { AlertIcon } from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type { ChainResponse, TokenResponse } from "../../api/types";

export function ChainAssignmentForm({
  token,
  chains,
  submitting,
  error,
  onSubmit,
}: {
  readonly token: TokenResponse;
  readonly chains: ReadonlyArray<ChainResponse>;
  readonly submitting: boolean;
  readonly error: string | null;
  readonly onSubmit: (chainId: string | null) => void;
}): JSX.Element {
  const [assignedChainId, setAssignedChainId] = useState<string>(
    token.assigned_chain_id ?? "",
  );

  const selectedChain =
    assignedChainId === ""
      ? null
      : chains.find((chain) => chain.chain_id === assignedChainId) ?? null;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    onSubmit(assignedChainId === "" ? null : assignedChainId);
  }

  return (
    <form className="form" onSubmit={handleSubmit} noValidate>
      <p className="form__hint">
        Change which fallback chain this API key uses. Existing app code keeps
        sending the same bearer key.
      </p>

      <div className="field">
        <label htmlFor="token-chain-assignment">Routing chain</label>
        <select
          id="token-chain-assignment"
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

      <div className="routing-summary" aria-live="polite">
        <span className="routing-summary__label">Routing result</span>
        {selectedChain === null ? (
          <p>This API key will use Gozar's default chain selection.</p>
        ) : (
          <p>
            This API key will always enter <strong>{selectedChain.name}</strong>.
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
        <button type="submit" className="button button--primary" disabled={submitting}>
          {submitting ? (
            <>
              <Spinner label="Saving" size={18} />
              <span>Saving...</span>
            </>
          ) : (
            <span>Save routing</span>
          )}
        </button>
      </div>
    </form>
  );
}
