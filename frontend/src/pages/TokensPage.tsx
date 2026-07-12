import { useCallback, useEffect, useMemo, useState } from "react";

import {
  listTokenModels,
  listTokens,
  revealToken,
  revokeToken,
  setTokenChain,
  setTokenEnabled,
  setTokenLimit,
  testTokenRoute,
} from "../api/tokens";
import { listChains } from "../api/chains";
import { ApiError } from "../api/errors";
import {
  AlertIcon,
  BanIcon,
  ChainIcon,
  CheckIcon,
  CopyIcon,
  GaugeIcon,
  InboxIcon,
  KeyIcon,
  PlusIcon,
  PowerIcon,
  RefreshIcon,
  TokenIcon,
} from "../components/icons";
import { PageGuide } from "../components/PageGuide";
import { Spinner } from "../components/Spinner";
import { TableSkeleton } from "../components/Skeleton";
import type {
  ChainResponse,
  IssuedTokenResponse,
  TokenResponse,
  UsageLimitSpec,
} from "../api/types";
import { describeLimit } from "./accounts/format";
import { LimitForm } from "./accounts/LimitForm";
import { Modal } from "./accounts/Modal";
import { ChainAssignmentForm } from "./tokens/ChainAssignmentForm";
import { CreateTokenForm } from "./tokens/CreateTokenForm";
import { RevealKeyForm } from "./tokens/RevealKeyForm";
import { SecretReveal } from "./tokens/SecretReveal";
import { describeUsage, isRevoked, tokenStatusView } from "./tokens/format";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

/**
 * Gozar API key management view (Requirements 8.1, 8.3, 17.2): list issued API keys
 * with label, status, configured limit, and recorded usage -- never the secret --
 * and create, configure limits for, enable/disable, and revoke them.
 *
 * The secret is rendered after creation and after explicit password-confirmed
 * reveal by {@link SecretReveal}. Every async surface renders explicit loading,
 * empty, and error states. Icons are outline SVGs; there are no emoji.
 */
export function TokensPage(): JSX.Element {
  const [tokens, setTokens] = useState<ReadonlyArray<TokenResponse> | null>(null);
  const [chains, setChains] = useState<ReadonlyArray<ChainResponse>>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [issued, setIssued] = useState<IssuedTokenResponse | null>(null);

  const [limitTarget, setLimitTarget] = useState<TokenResponse | null>(null);
  const [limitSubmitting, setLimitSubmitting] = useState(false);
  const [limitError, setLimitError] = useState<string | null>(null);

  const [chainTarget, setChainTarget] = useState<TokenResponse | null>(null);
  const [chainSubmitting, setChainSubmitting] = useState(false);
  const [chainError, setChainError] = useState<string | null>(null);

  const [revealTarget, setRevealTarget] = useState<TokenResponse | null>(null);
  const [revealSubmitting, setRevealSubmitting] = useState(false);
  const [revealError, setRevealError] = useState<string | null>(null);

  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(null);
    try {
      const [tokenResult, chainResult] = await Promise.all([
        listTokens(),
        listChains(),
      ]);
      setTokens(tokenResult);
      setChains(chainResult);
    } catch (cause) {
      setLoadError(messageFor(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleCreated = useCallback(
    (token: IssuedTokenResponse): void => {
      setCreateOpen(false);
      setIssued(token);
      void load();
    },
    [load],
  );

  async function handleToggleEnabled(token: TokenResponse): Promise<void> {
    setActionError(null);
    setBusyId(token.token_id);
    const nextEnabled = token.status === "disabled";
    try {
      await setTokenEnabled(token.token_id, nextEnabled);
      await load();
    } catch (cause) {
      setActionError(messageFor(cause));
    } finally {
      setBusyId(null);
    }
  }

  async function handleRevoke(token: TokenResponse): Promise<void> {
    const confirmed = window.confirm(
      `Revoke API key "${token.label}"? Revocation is permanent and immediately rejects any request using it.`,
    );
    if (!confirmed) {
      return;
    }
    setActionError(null);
    setBusyId(token.token_id);
    try {
      await revokeToken(token.token_id);
      await load();
    } catch (cause) {
      setActionError(messageFor(cause));
    } finally {
      setBusyId(null);
    }
  }

  async function handleSubmitLimit(spec: UsageLimitSpec): Promise<void> {
    if (limitTarget === null) {
      return;
    }
    setLimitError(null);
    setLimitSubmitting(true);
    try {
      await setTokenLimit(limitTarget.token_id, spec);
      setLimitTarget(null);
      await load();
    } catch (cause) {
      setLimitError(messageFor(cause));
    } finally {
      setLimitSubmitting(false);
    }
  }

  async function handleSubmitChain(chainId: string | null): Promise<void> {
    if (chainTarget === null) {
      return;
    }
    setChainError(null);
    setChainSubmitting(true);
    try {
      await setTokenChain(chainTarget.token_id, chainId);
      setChainTarget(null);
      await load();
    } catch (cause) {
      setChainError(messageFor(cause));
    } finally {
      setChainSubmitting(false);
    }
  }

  async function handleReveal(
    password: string,
    existingApiKey?: string,
  ): Promise<void> {
    if (revealTarget === null) {
      return;
    }
    setRevealError(null);
    setRevealSubmitting(true);
    try {
      const revealed = await revealToken(
        revealTarget.token_id,
        password,
        existingApiKey,
      );
      setRevealTarget(null);
      setIssued(revealed);
      await load();
    } catch (cause) {
      setRevealError(messageFor(cause));
    } finally {
      setRevealSubmitting(false);
    }
  }

  return (
    <>
      <div className="toolbar">
        <p className="toolbar__lead">
          Create app-facing Gozar API keys and choose how each key routes traffic.
        </p>
        <button
          type="button"
          className="button button--primary"
          onClick={() => setCreateOpen(true)}
        >
          <PlusIcon size={18} aria-hidden />
          <span>Create API key</span>
        </button>
      </div>

      <PageGuide
        id="tokens-guide-title"
        title="Issue app API keys"
        description="Apps authenticate with a Gozar API key. The key stays stable while Gozar handles provider credentials, model discovery, fallbacks, limits, and traces."
        steps={[
          {
            title: "Create a key",
            description: "Copy the full key. Later you can reveal the same key with your password.",
            Icon: KeyIcon,
          },
          {
            title: "Choose a route",
            description: "Leave routing automatic or pin the key to a saved fallback chain.",
            Icon: ChainIcon,
          },
          {
            title: "Use the SDK",
            description: "Set base_url to this origin plus /v1 and use the Gozar key as api_key.",
            Icon: TokenIcon,
          },
        ]}
      />

      {actionError !== null && (
        <p className="alert alert--error page-alert" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{actionError}</span>
        </p>
      )}

      <TokensBody
        loading={loading}
        loadError={loadError}
        tokens={tokens}
        busyId={busyId}
        onRetry={() => void load()}
        onConfigureChain={(token) => {
          setChainError(null);
          setChainTarget(token);
        }}
        onConfigureLimit={(token) => {
          setLimitError(null);
          setLimitTarget(token);
        }}
        onReveal={(token) => {
          setRevealError(null);
          setRevealTarget(token);
        }}
        onToggleEnabled={(token) => void handleToggleEnabled(token)}
        onRevoke={(token) => void handleRevoke(token)}
      />

      {createOpen && (
        <Modal title="Create API key" size="wide" onClose={() => setCreateOpen(false)}>
          <CreateTokenForm chains={chains} onCreated={handleCreated} />
        </Modal>
      )}

      {issued !== null && (
        <Modal title="API key ready" onClose={() => setIssued(null)}>
          <SecretReveal issued={issued} onDone={() => setIssued(null)} />
        </Modal>
      )}

      {limitTarget !== null && (
        <Modal
          title={`Configure limit - ${limitTarget.label}`}
          onClose={() => setLimitTarget(null)}
        >
          <LimitForm
            initial={limitTarget.limit}
            submitting={limitSubmitting}
            error={limitError}
            onSubmit={(spec) => void handleSubmitLimit(spec)}
          />
        </Modal>
      )}

      {revealTarget !== null && (
        <Modal
          title={`Reveal API key - ${revealTarget.label}`}
          onClose={() => setRevealTarget(null)}
        >
          <RevealKeyForm
            token={revealTarget}
            submitting={revealSubmitting}
            error={revealError}
            onSubmit={(password, existingApiKey) =>
              void handleReveal(password, existingApiKey)
            }
          />
        </Modal>
      )}

      {chainTarget !== null && (
        <Modal
          title={`Routing - ${chainTarget.label}`}
          onClose={() => setChainTarget(null)}
        >
          <ChainAssignmentForm
            token={chainTarget}
            chains={chains}
            submitting={chainSubmitting}
            error={chainError}
            onSubmit={(chainId) => void handleSubmitChain(chainId)}
          />
        </Modal>
      )}

      <TokenIntegrationGuide tokens={tokens ?? []} />
    </>
  );
}

/** Renders the loading / error / empty / populated states of the token list. */
function TokensBody({
  loading,
  loadError,
  tokens,
  busyId,
  onRetry,
  onConfigureChain,
  onConfigureLimit,
  onReveal,
  onToggleEnabled,
  onRevoke,
}: {
  readonly loading: boolean;
  readonly loadError: string | null;
  readonly tokens: ReadonlyArray<TokenResponse> | null;
  readonly busyId: string | null;
  readonly onRetry: () => void;
  readonly onConfigureChain: (token: TokenResponse) => void;
  readonly onConfigureLimit: (token: TokenResponse) => void;
  readonly onReveal: (token: TokenResponse) => void;
  readonly onToggleEnabled: (token: TokenResponse) => void;
  readonly onRevoke: (token: TokenResponse) => void;
}): JSX.Element {
  if (loading && tokens === null) {
    return <TableSkeleton columns={6} label="Loading API keys..." />;
  }

  if (loadError !== null && tokens === null) {
    return (
      <div className="state state--error" role="alert">
        <AlertIcon size={22} aria-hidden />
        <p>{loadError}</p>
        <button type="button" className="button button--ghost" onClick={onRetry}>
          <RefreshIcon size={18} aria-hidden />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  if (tokens !== null && tokens.length === 0) {
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>No API keys yet.</p>
        <p className="state__hint">Create a key when an app is ready to call Gozar.</p>
      </div>
    );
  }

  const rows = tokens ?? [];

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Label</th>
            <th scope="col">Routing</th>
            <th scope="col">Status</th>
            <th scope="col">Limit</th>
            <th scope="col">Usage</th>
            <th scope="col" className="table__actions-col">
              Actions
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((token) => {
            const status = tokenStatusView(token.status);
            const busy = busyId === token.token_id;
            const revoked = isRevoked(token.status);
            const isDisabled = token.status === "disabled";
            return (
              <tr key={token.token_id}>
                <td>
                  <span className="cell-primary">{token.label}</span>
                </td>
                <td>
                  <TokenRoutingCell token={token} />
                </td>
                <td>
                  <span className={`badge badge--${status.tone}`}>{status.label}</span>
                </td>
                <td>{describeLimit(token.limit)}</td>
                <td>{describeUsage(token)}</td>
                <td>
                  <div className="row-actions">
                    {busy ? (
                      <Spinner label="Working" size={18} />
                    ) : revoked ? (
                      <span className="cell-secondary">No actions</span>
                    ) : (
                      <>
                        <button
                          type="button"
                          className="icon-button"
                          onClick={() => onConfigureChain(token)}
                          aria-label={`Configure routing for ${token.label}`}
                          title="Configure routing"
                        >
                          <ChainIcon size={18} />
                        </button>
                        <button
                          type="button"
                          className="icon-button"
                          onClick={() => onConfigureLimit(token)}
                          aria-label={`Configure limit for ${token.label}`}
                          title="Configure limit"
                        >
                          <GaugeIcon size={18} />
                        </button>
                        <button
                          type="button"
                          className="icon-button"
                          onClick={() => onReveal(token)}
                          aria-label={`Reveal API key for ${token.label}`}
                          title={
                            token.can_reveal === false
                              ? "Save existing key for future reveal"
                              : "Reveal key"
                          }
                        >
                          <KeyIcon size={18} />
                        </button>
                        <button
                          type="button"
                          className="icon-button"
                          onClick={() => onToggleEnabled(token)}
                          aria-label={`${isDisabled ? "Enable" : "Disable"} ${token.label}`}
                          title={isDisabled ? "Enable" : "Disable"}
                        >
                          <PowerIcon size={18} />
                        </button>
                        <button
                          type="button"
                          className="icon-button icon-button--danger"
                          onClick={() => onRevoke(token)}
                          aria-label={`Revoke ${token.label}`}
                          title="Revoke"
                        >
                          <BanIcon size={18} />
                        </button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TokenRoutingCell({ token }: { readonly token: TokenResponse }): JSX.Element {
  if (token.assigned_chain_id && token.assigned_chain_name) {
    return (
      <>
        <span className="cell-primary">{token.assigned_chain_name}</span>
        <span className="cell-secondary">Pinned chain</span>
      </>
    );
  }
  if (token.assigned_chain_id) {
    return (
      <>
        <span className="cell-primary">Pinned chain</span>
        <span className="cell-secondary">Chain name unavailable</span>
      </>
    );
  }
  return (
    <>
      <span className="cell-primary">Auto</span>
      <span className="cell-secondary">Default routing</span>
    </>
  );
}

type IntegrationSnippetId = "python" | "curl" | "langgraph";
type IntegrationSnippetLanguage = "http" | "shell" | "python";
type IntegrationTestResult =
  | { readonly tone: "success"; readonly title: string; readonly body: string }
  | { readonly tone: "error"; readonly title: string; readonly body: string };

interface IntegrationSnippet {
  readonly id: IntegrationSnippetId;
  readonly title: string;
  readonly eyebrow: string;
  readonly language: IntegrationSnippetLanguage;
  readonly code: string;
}

function getBrowserGozarBaseUrl(): string {
  if (typeof window === "undefined" || window.location.origin === "null") {
    return "http://localhost:8000/v1";
  }
  return `${window.location.origin.replace(/\/+$/, "")}/v1`;
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return null;
}

function extractAssistantText(payload: unknown): string {
  const root = objectRecord(payload);
  const choices = Array.isArray(root?.choices) ? root.choices : [];
  const first = objectRecord(choices[0]);
  const message = objectRecord(first?.message);
  const content = message?.content;
  if (typeof content === "string" && content.trim() !== "") {
    return content;
  }
  if (Array.isArray(content)) {
    const text = content
      .map((part) => objectRecord(part)?.text)
      .filter((part): part is string => typeof part === "string")
      .join("");
    if (text.trim() !== "") {
      return text;
    }
  }
  return "Request succeeded, but the response did not include assistant text.";
}

function buildIntegrationSnippets(
  gozarBaseUrl: string,
  gozarModel: string,
): readonly [IntegrationSnippet, ...IntegrationSnippet[]] {
  return [
    {
      id: "python",
      title: "OpenAI Python",
      eyebrow: "SDK",
      language: "python",
      code: `import os
from openai import OpenAI

os.environ.setdefault("GOZAR_BASE_URL", "${gozarBaseUrl}")
os.environ.setdefault("GOZAR_API_KEY", "gz-YOUR_GOZAR_API_KEY")
os.environ.setdefault("GOZAR_MODEL", "${gozarModel}")

client = OpenAI(
    base_url=os.environ["GOZAR_BASE_URL"],
    api_key=os.environ["GOZAR_API_KEY"],
)

response = client.chat.completions.create(
    model=os.environ["GOZAR_MODEL"],
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)`,
    },
    {
      id: "curl",
      title: "cURL",
      eyebrow: "Shell",
      language: "shell",
      code: `export GOZAR_BASE_URL="${gozarBaseUrl}"
export GOZAR_API_KEY="gz-YOUR_GOZAR_API_KEY"
export GOZAR_MODEL="${gozarModel}"

curl "$GOZAR_BASE_URL/chat/completions" \\
  -H "Authorization: Bearer $GOZAR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d "{
    \\"model\\": \\"$GOZAR_MODEL\\",
    \\"messages\\": [
      {\\"role\\": \\"user\\", \\"content\\": \\"Hello\\"}
    ]
  }"`,
    },
    {
      id: "langgraph",
      title: "LangGraph node",
      eyebrow: "LangChain",
      language: "python",
      code: `import os
from langchain_openai import ChatOpenAI

os.environ.setdefault("GOZAR_BASE_URL", "${gozarBaseUrl}")
os.environ.setdefault("GOZAR_API_KEY", "gz-YOUR_GOZAR_API_KEY")
os.environ.setdefault("GOZAR_MODEL", "${gozarModel}")

llm = ChatOpenAI(
    model=os.environ["GOZAR_MODEL"],
    base_url=os.environ["GOZAR_BASE_URL"],
    api_key=os.environ["GOZAR_API_KEY"],
)

def llm_node(state):
    message = llm.invoke(state["messages"])
    return {"messages": [message]}`,
    },
  ];
}

interface HighlightSegment {
  readonly text: string;
  readonly kind: "plain" | "keyword" | "string" | "variable" | "property" | "number" | "comment" | "command";
}

const PYTHON_KEYWORDS = new Set([
  "def",
  "from",
  "import",
  "return",
]);

const SHELL_COMMANDS = new Set(["curl", "export"]);
const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

function highlightLine(line: string, language: IntegrationSnippetLanguage): ReadonlyArray<HighlightSegment> {
  if (language === "python") {
    return highlightPythonLine(line);
  }

  if (language === "shell") {
    return highlightShellLine(line);
  }

  return highlightHttpLine(line);
}

function highlightPythonLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;

  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);

    if (char === "#") {
      segments.push({ text: rest, kind: "comment" });
      break;
    }

    if (char === "\"" || char === "'") {
      const end = findStringEnd(line, index, char);
      segments.push({ text: line.slice(index, end), kind: "string" });
      index = end;
      continue;
    }

    const envMatch = rest.match(/^[A-Z][A-Z0-9_]+/);
    if (envMatch !== null) {
      segments.push({ text: envMatch[0], kind: "variable" });
      index += envMatch[0].length;
      continue;
    }

    const wordMatch = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (wordMatch !== null) {
      const word = wordMatch[0];
      segments.push({
        text: word,
        kind: PYTHON_KEYWORDS.has(word) ? "keyword" : "plain",
      });
      index += word.length;
      continue;
    }

    const numberMatch = rest.match(/^\d+(?:\.\d+)?/);
    if (numberMatch !== null) {
      segments.push({ text: numberMatch[0], kind: "number" });
      index += numberMatch[0].length;
      continue;
    }

    segments.push({ text: char, kind: "plain" });
    index += 1;
  }

  return segments;
}

function highlightShellLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;

  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);

    if (char === "#") {
      segments.push({ text: rest, kind: "comment" });
      break;
    }

    if (char === "\"" || char === "'") {
      const end = findStringEnd(line, index, char);
      segments.push({ text: line.slice(index, end), kind: "string" });
      index = end;
      continue;
    }

    const variableMatch = rest.match(/^\$[A-Z][A-Z0-9_]*/);
    if (variableMatch !== null) {
      segments.push({ text: variableMatch[0], kind: "variable" });
      index += variableMatch[0].length;
      continue;
    }

    const envMatch = rest.match(/^[A-Z][A-Z0-9_]+(?==)/);
    if (envMatch !== null) {
      segments.push({ text: envMatch[0], kind: "variable" });
      index += envMatch[0].length;
      continue;
    }

    const flagMatch = rest.match(/^-[A-Za-z]/);
    if (flagMatch !== null) {
      segments.push({ text: flagMatch[0], kind: "property" });
      index += flagMatch[0].length;
      continue;
    }

    const wordMatch = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (wordMatch !== null) {
      const word = wordMatch[0];
      segments.push({
        text: word,
        kind: SHELL_COMMANDS.has(word) ? "command" : "plain",
      });
      index += word.length;
      continue;
    }

    segments.push({ text: char, kind: "plain" });
    index += 1;
  }

  return segments;
}

function highlightHttpLine(line: string): ReadonlyArray<HighlightSegment> {
  const method = line.split(" ", 1)[0] ?? "";
  if (HTTP_METHODS.has(method)) {
    return [
      { text: method, kind: "keyword" },
      { text: line.slice(method.length), kind: "string" },
    ];
  }

  const headerMatch = line.match(/^([A-Za-z-]+:)(.*)$/);
  if (headerMatch !== null) {
    return [
      { text: headerMatch[1] ?? "", kind: "property" },
      ...highlightPlaceholders(headerMatch[2] ?? ""),
    ];
  }

  return highlightJsonishLine(line);
}

function highlightJsonishLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;

  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);

    const placeholderMatch = rest.match(/^\{\{[A-Z0-9_]+\}\}/);
    if (placeholderMatch !== null) {
      segments.push({ text: placeholderMatch[0], kind: "variable" });
      index += placeholderMatch[0].length;
      continue;
    }

    if (char === "\"") {
      const end = findStringEnd(line, index, "\"");
      const next = line.slice(end).trimStart();
      segments.push({
        text: line.slice(index, end),
        kind: next.startsWith(":") ? "property" : "string",
      });
      index = end;
      continue;
    }

    const numberMatch = rest.match(/^\d+(?:\.\d+)?/);
    if (numberMatch !== null) {
      segments.push({ text: numberMatch[0], kind: "number" });
      index += numberMatch[0].length;
      continue;
    }

    segments.push({ text: char, kind: "plain" });
    index += 1;
  }

  return segments;
}

function highlightPlaceholders(text: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;

  while (index < text.length) {
    const match = text.slice(index).match(/^\{\{[A-Z0-9_]+\}\}/);
    if (match !== null) {
      segments.push({ text: match[0], kind: "variable" });
      index += match[0].length;
      continue;
    }

    segments.push({ text: text[index] ?? "", kind: "plain" });
    index += 1;
  }

  return segments;
}

function findStringEnd(line: string, start: number, quote: string): number {
  let index = start + 1;
  while (index < line.length) {
    if (line[index] === "\\" && index + 1 < line.length) {
      index += 2;
      continue;
    }
    if (line[index] === quote) {
      return index + 1;
    }
    index += 1;
  }
  return line.length;
}

function HighlightedCode({
  code,
  language,
}: {
  readonly code: string;
  readonly language: IntegrationSnippetLanguage;
}): JSX.Element {
  return (
    <code>
      {code.split("\n").map((line, lineIndex) => (
        <span className="snippet__line" key={`${lineIndex}-${line}`}>
          {highlightLine(line, language).map((segment, segmentIndex) => (
            <span
              className={`snippet__token snippet__token--${segment.kind}`}
              key={`${lineIndex}-${segmentIndex}-${segment.text}`}
            >
              {segment.text}
            </span>
          ))}
        </span>
      ))}
    </code>
  );
}

function describeIntegrationRoute(token: TokenResponse | null): string {
  if (token === null) {
    return "Create an active API key first";
  }
  if (token.assigned_chain_name) {
    return `${token.label} routes through ${token.assigned_chain_name}`;
  }
  if (token.assigned_chain_id) {
    return `${token.label} routes through a pinned chain`;
  }
  return `${token.label} uses automatic routing`;
}

function TokenIntegrationGuide({
  tokens,
}: {
  readonly tokens: ReadonlyArray<TokenResponse>;
}): JSX.Element {
  const [selectedId, setSelectedId] = useState<IntegrationSnippetId>("python");
  const [copiedId, setCopiedId] = useState<IntegrationSnippetId | null>(null);
  const [selectedTokenId, setSelectedTokenId] = useState<string>("");
  const [modelIds, setModelIds] = useState<ReadonlyArray<string>>([]);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [testPrompt, setTestPrompt] = useState("Hello from Gozar");
  const [testLoading, setTestLoading] = useState(false);
  const [testResult, setTestResult] = useState<IntegrationTestResult | null>(null);
  const gozarBaseUrl = getBrowserGozarBaseUrl();
  const gozarModel = selectedModelId || modelIds[0] || "MODEL_FROM_V1_MODELS";
  const integrationSnippets = buildIntegrationSnippets(gozarBaseUrl, gozarModel);
  const selected =
    integrationSnippets.find((snippet) => snippet.id === selectedId) ??
    integrationSnippets[0];
  const selectableTokens = useMemo(() => {
    const nonRevoked = tokens.filter((token) => !isRevoked(token.status));
    const active = nonRevoked.filter((token) => token.status === "active");
    return active.length > 0 ? active : nonRevoked;
  }, [tokens]);
  const routeToken =
    selectableTokens.find((token) => token.token_id === selectedTokenId) ??
    selectableTokens[0] ??
    null;
  const routeTokenId = routeToken?.token_id ?? "";
  const clipboardAvailable =
    typeof navigator !== "undefined" && navigator.clipboard !== undefined;

  useEffect(() => {
    if (selectableTokens.length === 0) {
      if (selectedTokenId !== "") {
        setSelectedTokenId("");
      }
      return;
    }

    if (!selectableTokens.some((token) => token.token_id === selectedTokenId)) {
      setSelectedTokenId(selectableTokens[0]?.token_id ?? "");
    }
  }, [selectableTokens, selectedTokenId]);

  useEffect(() => {
    if (routeTokenId === "") {
      setModelIds([]);
      setSelectedModelId("");
      setModelsError(null);
      setModelsLoading(false);
      return undefined;
    }

    let cancelled = false;
    setModelsLoading(true);
    setModelsError(null);

    listTokenModels(routeTokenId)
      .then((listing) => {
        if (cancelled) {
          return;
        }
        const ids = listing.data.map((model) => model.id).filter(Boolean);
        setModelIds(ids);
        setSelectedModelId((current) =>
          ids.includes(current) ? current : ids[0] ?? "",
        );
      })
      .catch((cause) => {
        if (cancelled) {
          return;
        }
        setModelIds([]);
        setSelectedModelId("");
        setModelsError(messageFor(cause));
      })
      .finally(() => {
        if (!cancelled) {
          setModelsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [routeTokenId]);

  useEffect(() => {
    if (copiedId === null) {
      return undefined;
    }

    const timer = window.setTimeout(() => {
      setCopiedId(null);
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [copiedId]);

  async function handleCopy(): Promise<void> {
    if (!clipboardAvailable) {
      return;
    }

    await navigator.clipboard.writeText(selected.code);
    setCopiedId(selected.id);
  }

  async function handleTestRequest(): Promise<void> {
    const prompt = testPrompt.trim() || "Hello";
    if (routeTokenId === "" || gozarModel === "MODEL_FROM_V1_MODELS") {
      return;
    }

    setTestLoading(true);
    setTestResult(null);
    try {
      const payload = await testTokenRoute(routeTokenId, {
        model: gozarModel,
        prompt,
      });
      setTestResult({
        tone: "success",
        title: "Request succeeded",
        body: extractAssistantText(payload),
      });
    } catch (cause) {
      setTestResult({
        tone: "error",
        title: "Request failed",
        body: cause instanceof ApiError ? cause.message : "The route test could not be completed.",
      });
    } finally {
      setTestLoading(false);
    }
  }

  return (
    <section className="integration-guide" aria-labelledby="token-integration-title">
      <div className="integration-guide__head">
        <div>
          <h2 id="token-integration-title" className="integration-guide__title">
            Use in your app
          </h2>
          <p className="integration-guide__lead">
            Use the base URL from this browser, choose the API key route you want
            to document, and Gozar fills the model from that key's reachable models.
          </p>
        </div>
      </div>

      <div className="integration-setup">
        <dl className="integration-vars" aria-label="Integration variables">
          <div>
            <dt>Base URL</dt>
            <dd>
              <code>GOZAR_BASE_URL={gozarBaseUrl}</code>
            </dd>
          </div>
        </dl>

        <div className="integration-route-picker">
          <div className="field">
            <label htmlFor="integration-token-select">Example API key</label>
            <select
              id="integration-token-select"
              value={routeTokenId}
              onChange={(event) => setSelectedTokenId(event.target.value)}
              disabled={selectableTokens.length === 0}
            >
              {selectableTokens.length === 0 ? (
                <option value="">No active API keys</option>
              ) : (
                selectableTokens.map((token) => (
                  <option key={token.token_id} value={token.token_id}>
                    {token.label} - {token.id_prefix}...
                  </option>
                ))
              )}
            </select>
          </div>
          <div className="field">
            <label htmlFor="integration-model-select">Model</label>
            <select
              id="integration-model-select"
              value={selectedModelId}
              onChange={(event) => setSelectedModelId(event.target.value)}
              disabled={modelsLoading || modelIds.length === 0}
            >
              {modelIds.length === 0 ? (
                <option value="">No reachable models</option>
              ) : (
                modelIds.map((modelId) => (
                  <option key={modelId} value={modelId}>
                    {modelId}
                  </option>
                ))
              )}
            </select>
          </div>
          <div className="integration-route-card">
            <span>Selected route</span>
            <strong>{describeIntegrationRoute(routeToken)}</strong>
          </div>
        </div>
      </div>

      {modelsLoading && (
        <p className="integration-note" role="status">
          Loading reachable models for the selected API key...
        </p>
      )}
      {modelsError !== null && (
        <p className="alert alert--warn">
          <AlertIcon size={18} aria-hidden />
          <span>
            Could not load models from the selected API key. Refresh the route or
            check /v1/models with that key before copying examples.
          </span>
        </p>
      )}

      <div className="integration-test" aria-labelledby="integration-test-title">
        <div className="integration-test__copy">
          <h3 id="integration-test-title">Test this route</h3>
          <p>
            Gozar tests the selected API key internally; its secret never needs to be pasted or
            exposed in this browser. The request uses {modelIds.length > 0 ? gozarModel : "the selected model"}.
          </p>
        </div>
        <form
          className="integration-test__form"
          autoComplete="off"
          onSubmit={(event) => {
            event.preventDefault();
            void handleTestRequest();
          }}
        >
          <div className="field integration-test__field">
            <label htmlFor="integration-test-prompt">Prompt</label>
            <input
              id="integration-test-prompt"
              type="text"
              value={testPrompt}
              onChange={(event) => setTestPrompt(event.target.value)}
              disabled={testLoading}
            />
          </div>
          <button
            type="submit"
            className="button button--primary integration-test__button"
            disabled={
              testLoading ||
              routeTokenId === "" ||
              gozarModel === "MODEL_FROM_V1_MODELS"
            }
          >
            {testLoading ? <Spinner label="Sending test request" size={16} /> : <TokenIcon size={16} />}
            <span>{testLoading ? "Sending..." : "Send test request"}</span>
          </button>
        </form>
        {testResult !== null && (
          <div
            className={`integration-test__result integration-test__result--${testResult.tone}`}
            role={testResult.tone === "error" ? "alert" : "status"}
          >
            <strong>{testResult.title}</strong>
            <pre>{testResult.body}</pre>
          </div>
        )}
      </div>

      <div className="integration-panel">
        <div className="integration-tabs" role="tablist" aria-label="Integration examples">
          {integrationSnippets.map((snippet) => (
            <button
              key={snippet.id}
              id={`integration-tab-${snippet.id}`}
              type="button"
              role="tab"
              aria-selected={snippet.id === selected.id}
              aria-controls={`integration-panel-${snippet.id}`}
              className={
                snippet.id === selected.id
                  ? "integration-tab integration-tab--active"
                  : "integration-tab"
              }
              onClick={() => setSelectedId(snippet.id)}
            >
              <span>{snippet.title}</span>
              <small>{snippet.eyebrow}</small>
            </button>
          ))}
        </div>

        <div className="integration-example">
          <div className="integration-example__bar">
            <div>
              <span className="integration-example__eyebrow">{selected.eyebrow}</span>
              <h3 className="integration-example__title">{selected.title}</h3>
            </div>
            <button
              type="button"
              className="button button--ghost integration-example__copy"
              onClick={() => void handleCopy()}
              disabled={!clipboardAvailable}
              aria-label={`Copy ${selected.title} snippet`}
            >
              {copiedId === selected.id ? <CheckIcon size={16} /> : <CopyIcon size={16} />}
              <span>{copiedId === selected.id ? "Copied" : "Copy"}</span>
            </button>
          </div>
          <pre
            id={`integration-panel-${selected.id}`}
            className="snippet__code"
            role="tabpanel"
            aria-labelledby={`integration-tab-${selected.id}`}
          >
            <HighlightedCode code={selected.code} language={selected.language} />
          </pre>
        </div>
      </div>
    </section>
  );
}
