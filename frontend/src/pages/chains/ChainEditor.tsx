import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  AlertIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  InboxIcon,
  PlusIcon,
  TrashIcon,
} from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type {
  AccountResponse,
  ChainResponse,
  FallbackPolicy,
} from "../../api/types";
import { statusView } from "../accounts/format";
import { providerLabel } from "../accounts/providers";
import { entryAvailability } from "./format";

export interface ChainDraftEntry {
  readonly account_id: string;
  readonly model: string | null;
  readonly fallback_policy: FallbackPolicy;
}

export interface ChainDraft {
  readonly name: string;
  readonly entries: ReadonlyArray<ChainDraftEntry>;
  readonly model_selector: string | null;
}

type ChainFlowNode = Node<{ readonly label: ReactNode }>;
type ChainFlowInstance = ReactFlowInstance<ChainFlowNode, Edge>;

const FLOW_FIT_OPTIONS = { padding: 0.24 } as const;

function makeFlowLabel({
  eyebrow,
  title,
  meta,
}: {
  readonly eyebrow: string;
  readonly title: string;
  readonly meta: string;
}): JSX.Element {
  return (
    <span className="chain-flow-node__content">
      <span className="chain-flow-node__eyebrow">{eyebrow}</span>
      <span className="chain-flow-node__title">{title}</span>
      <span className="chain-flow-node__meta">{meta}</span>
    </span>
  );
}

function normalizedModel(value: string): string | null {
  const model = value.trim();
  return model === "" ? null : model;
}

export function ChainEditor({
  initial,
  accounts,
  accountsById,
  modelsByAccount,
  submitting,
  error,
  onSubmit,
}: {
  readonly initial: ChainResponse | null;
  readonly accounts: ReadonlyArray<AccountResponse>;
  readonly accountsById: ReadonlyMap<string, AccountResponse>;
  readonly modelsByAccount: ReadonlyMap<string, ReadonlyArray<string>>;
  readonly submitting: boolean;
  readonly error: string | null;
  readonly onSubmit: (draft: ChainDraft) => void;
}): JSX.Element {
  const [name, setName] = useState(initial?.name ?? "");
  const [modelSelector] = useState(initial?.model_selector ?? "");
  const [entries, setEntries] = useState<ReadonlyArray<ChainDraftEntry>>(() =>
    initial
      ? [...initial.entries]
          .sort((a, b) => a.position - b.position)
          .map((entry) => ({
            account_id: entry.account_id,
            model: entry.model?.trim() || null,
            fallback_policy: entry.fallback_policy ?? "any_error",
          }))
      : [],
  );
  const [picked, setPicked] = useState("");
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(
    initial?.entries[0]?.account_id ?? null,
  );
  const [flowInstance, setFlowInstance] = useState<ChainFlowInstance | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const entryIds = useMemo(() => entries.map((entry) => entry.account_id), [entries]);
  const addable = useMemo(
    () => accounts.filter((account) => !entryIds.includes(account.account_id)),
    [accounts, entryIds],
  );
  const selectedIndex = selectedEntryId
    ? entries.findIndex((entry) => entry.account_id === selectedEntryId)
    : -1;
  const selectedEntry = selectedIndex >= 0 ? entries[selectedIndex] ?? null : null;
  const selectedAccount = selectedEntry
    ? accountsById.get(selectedEntry.account_id) ?? null
    : null;
  const selectedAvailability = selectedEntry
    ? entryAvailability(selectedEntry.account_id, accountsById)
    : null;
  const selectedModels = selectedEntry
    ? modelsByAccount.get(selectedEntry.account_id) ?? []
    : [];

  function moveEntry(index: number, delta: number): void {
    const target = index + delta;
    if (target < 0 || target >= entries.length) {
      return;
    }
    const next = [...entries];
    const [moved] = next.splice(index, 1);
    if (moved === undefined) {
      return;
    }
    next.splice(target, 0, moved);
    setEntries(next);
  }

  function removeEntry(index: number): void {
    const removed = entries[index];
    const next = entries.filter((_, candidate) => candidate !== index);
    setEntries(next);
    if (removed?.account_id === selectedEntryId) {
      setSelectedEntryId(next[index]?.account_id ?? next[index - 1]?.account_id ?? null);
    }
  }

  function addPicked(): void {
    if (picked === "" || entryIds.includes(picked)) {
      return;
    }
    const defaultModel = modelsByAccount.get(picked)?.[0] ?? null;
    setEntries([
      ...entries,
      { account_id: picked, model: defaultModel, fallback_policy: "any_error" },
    ]);
    setSelectedEntryId(picked);
    setPicked("");
  }

  function updateSelectedModel(value: string): void {
    if (selectedEntry === null) {
      return;
    }
    const model = normalizedModel(value);
    setEntries((current) =>
      current.map((entry) =>
        entry.account_id === selectedEntry.account_id ? { ...entry, model } : entry,
      ),
    );
  }

  function updateSelectedPolicy(fallbackPolicy: FallbackPolicy): void {
    if (selectedEntry === null) {
      return;
    }
    setEntries((current) =>
      current.map((entry) =>
        entry.account_id === selectedEntry.account_id
          ? { ...entry, fallback_policy: fallbackPolicy }
          : entry,
      ),
    );
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setLocalError(null);
    const trimmedName = name.trim();
    if (trimmedName === "") {
      setLocalError("Enter a name for the chain.");
      return;
    }
    if (entries.length === 0) {
      setLocalError("Add at least one account to the chain.");
      return;
    }
    onSubmit({
      name: trimmedName,
      entries,
      model_selector: normalizedModel(modelSelector),
    });
  }

  const chainLabel = modelSelector.trim() || "API key route";
  const flowLayoutKey = `${entries
    .map((entry) => `${entry.account_id}:${entry.model ?? "request"}`)
    .join("|")}::${chainLabel}`;
  const flowNodes = useMemo<ChainFlowNode[]>(() => {
    const nodeGap = 214;
    const baseY = 84;
    const accountNodes = entries.map<ChainFlowNode>((entry, index) => {
      const availability = entryAvailability(entry.account_id, accountsById);
      const provider = availability.provider ? providerLabel(availability.provider) : "Unknown";
      return {
        id: entry.account_id,
        type: "default",
        position: { x: (index + 1) * nodeGap, y: baseY },
        data: {
          label: makeFlowLabel({
            eyebrow: index === 0 ? "Primary" : `Fallback ${index}`,
            title: availability.label,
            meta: `${provider} - ${entry.model ?? "Request model"}`,
          }),
        },
        className: availability.available
          ? "chain-flow-node"
          : "chain-flow-node chain-flow-node--unavailable",
        selected: selectedEntryId === entry.account_id,
      };
    });
    return [
      {
        id: "request",
        type: "input",
        position: { x: 0, y: baseY },
        data: {
          label: makeFlowLabel({ eyebrow: "Input", title: "LLM call", meta: chainLabel }),
        },
        className: "chain-flow-node chain-flow-node--system",
      },
      ...accountNodes,
      {
        id: "complete",
        type: "output",
        position: { x: (entries.length + 1) * nodeGap, y: baseY },
        data: {
          label: makeFlowLabel({
            eyebrow: "Result",
            title: "First success",
            meta: "Stops fallback",
          }),
        },
        className: "chain-flow-node chain-flow-node--system",
      },
    ];
  }, [accountsById, chainLabel, entries, selectedEntryId]);

  const flowEdges = useMemo<Edge[]>(() => {
    const nodeIds = ["request", ...entryIds, "complete"];
    return nodeIds.slice(0, -1).map((source, index) => ({
      id: `${source}-${nodeIds[index + 1] ?? "complete"}`,
      source,
      target: nodeIds[index + 1] ?? "complete",
      type: "smoothstep",
      animated: true,
      markerEnd: { type: MarkerType.ArrowClosed },
      className: "chain-flow-edge",
    }));
  }, [entryIds]);

  useEffect(() => {
    if (flowInstance === null || entries.length === 0) {
      return;
    }
    const timers = [0, 180, 520].map((delay) =>
      window.setTimeout(() => void flowInstance.fitView(FLOW_FIT_OPTIONS), delay),
    );
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [entries.length, flowInstance, flowLayoutKey]);

  useEffect(() => {
    if (flowInstance === null) {
      return;
    }
    let timer: number | null = null;
    const refit = (): void => {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
      timer = window.setTimeout(() => void flowInstance.fitView(FLOW_FIT_OPTIONS), 80);
    };
    window.addEventListener("resize", refit);
    return () => {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
      window.removeEventListener("resize", refit);
    };
  }, [flowInstance]);

  const message = localError ?? error;

  return (
    <form className="form" onSubmit={handleSubmit} noValidate>
      <div className="chain-editor-grid">
        <div className="field">
          <label htmlFor="chain-name">Name</label>
          <input
            id="chain-name"
            type="text"
            autoComplete="off"
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={submitting}
            required
          />
          <p className="field__hint">Use a stable workflow name, such as support-production.</p>
        </div>
      </div>

      <fieldset className="chain-entries" disabled={submitting}>
        <legend className="chain-entries__legend">
          Routing path
          <span className="chain-entries__hint">
            Each step owns its provider account and model. Failures continue downward.
          </span>
        </legend>

        <div className="chain-designer">
          <section className="chain-flow-panel" aria-label="Routing graph preview">
            {entries.length === 0 ? (
              <div className="chain-entries__empty">
                <InboxIcon size={22} aria-hidden />
                <p>Add the primary account to begin.</p>
              </div>
            ) : (
              <ReactFlow
                nodes={flowNodes}
                edges={flowEdges}
                fitView
                fitViewOptions={FLOW_FIT_OPTIONS}
                minZoom={0.25}
                maxZoom={1.2}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable
                onInit={setFlowInstance}
                onNodeClick={(_, node) => {
                  if (node.id !== "request" && node.id !== "complete") {
                    setSelectedEntryId(node.id);
                  }
                }}
                proOptions={{ hideAttribution: true }}
              >
                <Background gap={24} size={1} />
                <Controls showInteractive={false} />
              </ReactFlow>
            )}
          </section>

          <aside className="chain-detail-panel" aria-live="polite">
            <span className="chain-detail-panel__eyebrow">Node settings</span>
            {selectedEntry === null || selectedAvailability === null ? (
              <>
                <h3>No node selected</h3>
                <p>Add an account, then choose the exact model used at that step.</p>
              </>
            ) : (
              <>
                <div className="chain-detail-panel__title-row">
                  <div>
                    <h3>{selectedAvailability.label}</h3>
                    <p>
                      Step {selectedIndex + 1} - {selectedIndex === 0 ? "Primary" : "Fallback"}
                    </p>
                  </div>
                  <span
                    className={`badge badge--${
                      selectedAvailability.available ? "ok" : selectedAvailability.tone
                    }`}
                  >
                    {selectedAvailability.available
                      ? "Available"
                      : selectedAvailability.reason ?? "Unavailable"}
                  </span>
                </div>

                <dl className="chain-detail-list">
                  <div>
                    <dt>Provider</dt>
                    <dd>
                      {selectedAvailability.provider
                        ? providerLabel(selectedAvailability.provider)
                        : "Unknown"}
                    </dd>
                  </div>
                  <div>
                    <dt>Account</dt>
                    <dd>{selectedAccount ? statusView(selectedAccount.status).label : "Deleted"}</dd>
                  </div>
                </dl>

                <div className="field chain-node-model-field">
                  <label htmlFor="chain-node-model">Model for this node</label>
                  <input
                    id="chain-node-model"
                    list="chain-node-model-options"
                    value={selectedEntry.model ?? ""}
                    onChange={(event) => updateSelectedModel(event.target.value)}
                    placeholder="Use request model"
                    autoComplete="off"
                  />
                  <datalist id="chain-node-model-options">
                    {selectedModels.map((model) => (
                      <option key={model} value={model} />
                    ))}
                  </datalist>
                  <p className="field__hint">
                    Choose a listed model or enter an exact provider model ID. Leave blank to pass
                    through the request model.
                  </p>
                  {selectedModels.length === 0 && (
                    <p className="chain-node-model-field__warning">
                      No models are currently advertised for this account. Manual IDs are still
                      accepted and will be checked on the next catalog refresh.
                    </p>
                  )}
                </div>

                <div className="field chain-node-policy-field">
                  <label htmlFor="chain-node-policy">Fallback when this node fails</label>
                  <select
                    id="chain-node-policy"
                    value={selectedEntry.fallback_policy}
                    onChange={(event) =>
                      updateSelectedPolicy(event.target.value as FallbackPolicy)
                    }
                  >
                    <option value="any_error">Any provider failure</option>
                    <option value="auth_or_retryable">Auth or temporary failure</option>
                    <option value="retryable">Temporary failures only</option>
                  </select>
                  <p className="field__hint">
                    Temporary: network, 429, or 5xx. Auth: 401/403 after one refresh attempt.
                  </p>
                </div>
              </>
            )}
          </aside>

          {entries.length > 0 && (
            <ol className="chain-waterfall-list" aria-label="Fallback waterfall order">
              {entries.map((entry, index) => {
                const availability = entryAvailability(entry.account_id, accountsById);
                const selected = selectedEntryId === entry.account_id;
                return (
                  <li
                    key={entry.account_id}
                    className={`chain-waterfall${selected ? " chain-waterfall--selected" : ""}${
                      availability.available ? "" : " chain-waterfall--unavailable"
                    }`}
                  >
                    <button
                      type="button"
                      className="chain-waterfall__select"
                      onClick={() => setSelectedEntryId(entry.account_id)}
                      aria-label={`Select ${availability.label}`}
                    >
                      <span className="chain-waterfall__position">{index + 1}</span>
                      <span className="chain-waterfall__body">
                        <span className="cell-primary">{availability.label}</span>
                        <span className="cell-secondary">
                          {availability.provider ? providerLabel(availability.provider) : "Unknown"}
                          {" - "}
                          {entry.model ?? "Request model"}
                          {" - "}
                          {entry.fallback_policy === "retryable"
                            ? "Temporary errors"
                            : entry.fallback_policy === "auth_or_retryable"
                              ? "Auth or temporary errors"
                              : "Any error"}
                        </span>
                      </span>
                    </button>
                    <span className="chain-waterfall__actions">
                      <button
                        type="button"
                        className="icon-button"
                        onClick={() => moveEntry(index, -1)}
                        disabled={index === 0}
                        aria-label={`Move ${availability.label} up`}
                        title="Move up"
                      >
                        <ChevronUpIcon size={18} />
                      </button>
                      <button
                        type="button"
                        className="icon-button"
                        onClick={() => moveEntry(index, 1)}
                        disabled={index === entries.length - 1}
                        aria-label={`Move ${availability.label} down`}
                        title="Move down"
                      >
                        <ChevronDownIcon size={18} />
                      </button>
                      <button
                        type="button"
                        className="icon-button icon-button--danger"
                        onClick={() => removeEntry(index)}
                        aria-label={`Remove ${availability.label} from chain`}
                        title="Remove"
                      >
                        <TrashIcon size={18} />
                      </button>
                    </span>
                  </li>
                );
              })}
            </ol>
          )}
        </div>

        <div className="chain-entries__add">
          <div className="field chain-entries__picker">
            <label htmlFor="chain-add-account">Add account</label>
            <select
              id="chain-add-account"
              value={picked}
              onChange={(event) => setPicked(event.target.value)}
            >
              <option value="">
                {addable.length === 0 ? "No more accounts to add" : "Select an account..."}
              </option>
              {addable.map((account) => (
                <option key={account.account_id} value={account.account_id}>
                  {account.label} - {providerLabel(account.provider)}
                  {account.status !== "active" ? ` (${account.status})` : ""}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="button button--ghost"
            onClick={addPicked}
            disabled={picked === ""}
          >
            <PlusIcon size={18} aria-hidden />
            <span>Add node</span>
          </button>
        </div>
      </fieldset>

      {message !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{message}</span>
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
            <span>{initial ? "Save chain" : "Create chain"}</span>
          )}
        </button>
      </div>
    </form>
  );
}
