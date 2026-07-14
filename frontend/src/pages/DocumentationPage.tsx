import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  AccountsIcon,
  AlertIcon,
  ChainIcon,
  CheckIcon,
  CopyIcon,
  DocsIcon,
  ExternalLinkIcon,
  RefreshIcon,
  TokenIcon,
  TracesIcon,
} from "../components/icons";
import { SyntaxCode, type CodeLanguage } from "../components/SyntaxCode";
import { ROUTES } from "../routes";

type DocsSection = "overview" | "setup" | "chains" | "langgraph" | "api" | "ops";
type ExampleId =
  | "curl"
  | "python"
  | "embeddingsPython"
  | "typescript"
  | "langchainPython"
  | "langgraphPython"
  | "langchainTypescript"
  | "langgraphTypescript";

interface DocsTab {
  readonly id: DocsSection;
  readonly label: string;
  readonly summary: string;
}

interface IntegrationExample {
  readonly id: ExampleId;
  readonly label: string;
  readonly summary: string;
  readonly title: string;
  readonly language: CodeLanguage;
  readonly code: string;
}

const TABS: ReadonlyArray<DocsTab> = [
  { id: "overview", label: "Start here", summary: "Concepts and first path" },
  { id: "setup", label: "Setup", summary: "Accounts, chains, API keys" },
  { id: "chains", label: "Dynamic chains", summary: "Fallbacks and overrides" },
  { id: "langgraph", label: "LangGraph + SDKs", summary: "LLM invoke examples" },
  { id: "api", label: "API reference", summary: "Traffic and control endpoints" },
  { id: "ops", label: "Operations", summary: "Health, traces, model drift" },
];

function browserBaseUrl(): string {
  if (typeof window === "undefined" || window.location.origin === "null") {
    return "http://localhost:8000";
  }
  return window.location.origin.replace(/\/+$/, "");
}

function DocsCodeBlock({
  title,
  language,
  code,
}: {
  readonly title: string;
  readonly language: CodeLanguage;
  readonly code: string;
}): JSX.Element {
  const [copied, setCopied] = useState(false);

  async function copy(): Promise<void> {
    if (!navigator.clipboard) {
      return;
    }
    await navigator.clipboard.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="docs-code">
      <div className="docs-code__bar">
        <div>
          <span>{language}</span>
          <strong>{title}</strong>
        </div>
        <button type="button" className="icon-button" onClick={() => void copy()} title="Copy">
          {copied ? <CheckIcon size={17} /> : <CopyIcon size={17} />}
          <span className="sr-only">{copied ? "Copied" : "Copy code"}</span>
        </button>
      </div>
      <pre>
        <SyntaxCode code={code} language={language} />
      </pre>
    </div>
  );
}

function SectionHeading({
  number,
  title,
  children,
}: {
  readonly number: string;
  readonly title: string;
  readonly children: string;
}): JSX.Element {
  return (
    <div className="docs-heading">
      <span>{number}</span>
      <div>
        <h2>{title}</h2>
        <p>{children}</p>
      </div>
    </div>
  );
}

export function DocumentationPage(): JSX.Element {
  const [active, setActive] = useState<DocsSection>("overview");
  const [selectedExample, setSelectedExample] = useState<ExampleId>("curl");
  const baseUrl = browserBaseUrl();
  const apiBaseUrl = `${baseUrl}/v1`;

  const snippets = useMemo(
    () => ({
      env: `export GOZAR_BASE_URL="${apiBaseUrl}"
export GOZAR_API_KEY="gz-YOUR_GOZAR_API_KEY"
export GOZAR_MODEL="MODEL_FROM_V1_MODELS"
export GOZAR_EMBEDDING_MODEL="PROVIDER_EMBEDDING_MODEL"
export GOZAR_CHAIN_ID="OPTIONAL_CHAIN_UUID"`,
      firstCurl: `curl "$GOZAR_BASE_URL/chat/completions" \\
  -H "Authorization: Bearer $GOZAR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "'"$GOZAR_MODEL"'",
    "messages": [
      {"role": "user", "content": "Hello from Gozar"}
    ]
  }'`,
      models: `curl "$GOZAR_BASE_URL/models" \\
  -H "Authorization: Bearer $GOZAR_API_KEY"`,
      embeddings: `curl "$GOZAR_BASE_URL/embeddings" \\
  -H "Authorization: Bearer $GOZAR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "'"$GOZAR_EMBEDDING_MODEL"'",
    "input": ["first document", "second document"],
    "encoding_format": "float"
  }'`,
      upsert: `curl -X PUT "${baseUrl}/api/chains/by-key/support-production" \\
  -H "Authorization: Bearer $GOZAR_ADMIN_ACCESS_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "Support production",
    "entries": [
      {
        "account_id": "OPENAI_ACCOUNT_ID",
        "model": "PRIMARY_CHAT_MODEL_ID",
        "fallback_policy": "auth_or_retryable",
        "route": "chat"
      },
      {
        "account_id": "OPENROUTER_ACCOUNT_ID",
        "model": "OPENROUTER_CHAT_MODEL_ID",
        "fallback_policy": "retryable",
        "route": "chat"
      },
      {
        "account_id": "OPENROUTER_ACCOUNT_ID",
        "model": "OPENROUTER_EMBEDDING_MODEL_ID",
        "fallback_policy": "retryable",
        "route": "embeddings"
      }
    ]
  }'`,
      overrideHeader: `curl "$GOZAR_BASE_URL/chat/completions" \\
  -H "Authorization: Bearer $GOZAR_API_KEY" \\
  -H "X-Gozar-Chain-ID: $GOZAR_CHAIN_ID" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "route-input",
    "messages": [{"role": "user", "content": "Use this chain once"}]
  }'`,
      overrideBody: `{
  "model": "route-input",
  "messages": [{"role": "user", "content": "Use this chain once"}],
  "gozar": {
    "chain_id": "CHAIN_UUID"
  }
}`,
      routeTest: `curl -X POST "${baseUrl}/api/tokens/TOKEN_ID/test" \\
  -H "Authorization: Bearer $GOZAR_ADMIN_ACCESS_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "MODEL_FROM_SELECTED_ROUTE",
    "prompt": "Return one short sentence."
  }'`,
      traceMetadata: `curl -i "$GOZAR_BASE_URL/chat/completions" \\
  -H "Authorization: Bearer $GOZAR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "'"$GOZAR_MODEL"'",
    "messages": [{"role": "user", "content": "Show routing metadata"}],
    "gozar": {"include_metadata": true}
  }'`,
    }),
    [apiBaseUrl, baseUrl],
  );

  const examples = useMemo<ReadonlyArray<IntegrationExample>>(
    () => [
      {
        id: "curl",
        label: "cURL",
        summary: "Smallest possible request.",
        title: "Chat Completions over HTTP",
        language: "shell",
        code: snippets.firstCurl,
      },
      {
        id: "python",
        label: "OpenAI Python",
        summary: "Drop-in OpenAI SDK client.",
        title: "Python SDK with Gozar base URL",
        language: "python",
        code: `import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["GOZAR_BASE_URL"],
    api_key=os.environ["GOZAR_API_KEY"],
)

response = client.chat.completions.create(
    model=os.environ["GOZAR_MODEL"],
    messages=[{"role": "user", "content": "Hello from Python"}],
)

print(response.choices[0].message.content)`,
      },
      {
        id: "embeddingsPython",
        label: "Embeddings Python",
        summary: "Real vectors for RAG and memory.",
        title: "OpenAI embeddings SDK through Gozar",
        language: "python",
        code: `import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["GOZAR_BASE_URL"],
    api_key=os.environ["GOZAR_API_KEY"],
)

response = client.embeddings.create(
    model=os.environ["GOZAR_EMBEDDING_MODEL"],
    input=["first document", "second document"],
    encoding_format="float",
)

vectors = [item.embedding for item in response.data]
print(len(vectors), len(vectors[0]))`,
      },
      {
        id: "typescript",
        label: "OpenAI TypeScript",
        summary: "OpenAI JS SDK with one optional chain override.",
        title: "TypeScript SDK with optional chain header",
        language: "typescript",
        code: `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: process.env.GOZAR_BASE_URL,
  apiKey: process.env.GOZAR_API_KEY,
});

const chainHeaders = process.env.GOZAR_CHAIN_ID
  ? { "X-Gozar-Chain-ID": process.env.GOZAR_CHAIN_ID }
  : {};

const response = await client.chat.completions.create(
  {
    model: process.env.GOZAR_MODEL ?? "route-input",
    messages: [{ role: "user", content: "Hello from TypeScript" }],
  },
  { headers: chainHeaders },
);

console.log(response.choices[0]?.message?.content);`,
      },
      {
        id: "langchainPython",
        label: "LangChain Python",
        summary: "Use ChatOpenAI without changing your app shape.",
        title: "ChatOpenAI with invoke",
        language: "python",
        code: `import os
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model=os.environ["GOZAR_MODEL"],
    base_url=os.environ["GOZAR_BASE_URL"],
    api_key=os.environ["GOZAR_API_KEY"],
    default_headers={"X-Gozar-Chain-ID": os.environ.get("GOZAR_CHAIN_ID", "")},
    use_responses_api=False,
)

message = llm.invoke([{"role": "user", "content": "Hello from LangChain"}])
print(message.content)`,
      },
      {
        id: "langgraphPython",
        label: "LangGraph Python",
        summary: "Keep routing below the graph node.",
        title: "LangGraph node using llm.invoke",
        language: "python",
        code: `import os
from typing_extensions import TypedDict
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

class State(TypedDict):
    messages: list[dict[str, str]]

llm = ChatOpenAI(
    model=os.environ["GOZAR_MODEL"],
    base_url=os.environ["GOZAR_BASE_URL"],
    api_key=os.environ["GOZAR_API_KEY"],
    default_headers={"X-Gozar-Chain-ID": os.environ.get("GOZAR_CHAIN_ID", "")},
    use_responses_api=False,
)

def llm_node(state: State) -> State:
    return {"messages": [llm.invoke(state["messages"])]}

graph = (
    StateGraph(State)
    .add_node("llm", llm_node)
    .add_edge(START, "llm")
    .add_edge("llm", END)
    .compile()
)`,
      },
      {
        id: "langchainTypescript",
        label: "LangChain JS",
        summary: "TypeScript ChatOpenAI with custom base URL.",
        title: "LangChain JS invoke",
        language: "typescript",
        code: `import { ChatOpenAI } from "@langchain/openai";

const llm = new ChatOpenAI({
  model: process.env.GOZAR_MODEL ?? "route-input",
  configuration: {
    baseURL: process.env.GOZAR_BASE_URL,
    defaultHeaders: {
      "X-Gozar-Chain-ID": process.env.GOZAR_CHAIN_ID ?? "",
    },
  },
  streamUsage: false,
});

const message = await llm.invoke([
  { role: "user", content: "Hello from LangChain JS" },
]);

console.log(message.content);`,
      },
      {
        id: "langgraphTypescript",
        label: "LangGraph JS",
        summary: "A graph node that calls the selected Gozar route.",
        title: "LangGraph JS node",
        language: "typescript",
        code: `import { ChatOpenAI } from "@langchain/openai";
import { END, START, StateGraph, MessagesAnnotation } from "@langchain/langgraph";

const llm = new ChatOpenAI({
  model: process.env.GOZAR_MODEL ?? "route-input",
  configuration: {
    baseURL: process.env.GOZAR_BASE_URL,
    defaultHeaders: {
      "X-Gozar-Chain-ID": process.env.GOZAR_CHAIN_ID ?? "",
    },
  },
  streamUsage: false,
});

const graph = new StateGraph(MessagesAnnotation)
  .addNode("llm", async (state) => ({
    messages: [await llm.invoke(state.messages)],
  }))
  .addEdge(START, "llm")
  .addEdge("llm", END)
  .compile();`,
      },
    ],
    [snippets.firstCurl],
  );

  const selected =
    examples.find((example) => example.id === selectedExample) ??
    examples.find((example) => example.id === "curl")!;

  return (
    <div className="docs-page">
      <header className="docs-hero">
        <div className="docs-hero__icon">
          <DocsIcon size={24} />
        </div>
        <div>
          <p className="section-kicker">Gozar documentation</p>
          <h2>Operate one OpenAI-compatible gateway for many providers.</h2>
          <p>
            Start with the Console, create a stable Gozar API key, then call{" "}
            <code>{apiBaseUrl}</code> from cURL, OpenAI SDKs, LangChain, or LangGraph.
          </p>
        </div>
      </header>

      <div className="docs-layout">
        <nav className="docs-nav" aria-label="Documentation sections">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              className={active === tab.id ? "docs-nav__item docs-nav__item--active" : "docs-nav__item"}
              onClick={() => setActive(tab.id)}
              aria-current={active === tab.id ? "page" : undefined}
            >
              <strong>{tab.label}</strong>
              <span>{tab.summary}</span>
            </button>
          ))}
        </nav>

        <article className="docs-content">
          {active === "overview" && (
            <>
              <SectionHeading number="01" title="The shortest mental model">
                Accounts hold upstream credentials. Chains choose account and model order. API keys give your app a stable interface.
              </SectionHeading>

              <div className="docs-flow" aria-label="Gozar request flow">
                <div>
                  <AccountsIcon size={20} />
                  <strong>Account</strong>
                  <span>OpenAI, OpenRouter, Codex, or another provider credential.</span>
                </div>
                <div>
                  <ChainIcon size={20} />
                  <strong>Chain</strong>
                  <span>Independent LLM and Embeddings paths under one stable chain ID.</span>
                </div>
                <div>
                  <TokenIcon size={20} />
                  <strong>API key</strong>
                  <span>The stable key your application sends as Bearer auth.</span>
                </div>
                <div>
                  <TracesIcon size={20} />
                  <strong>Trace</strong>
                  <span>The audit trail showing which node answered and why.</span>
                </div>
              </div>

              <div className="docs-paths">
                <button type="button" onClick={() => setActive("setup")}>
                  <strong>I am new</strong>
                  <span>Follow the setup path from account to first request.</span>
                </button>
                <button type="button" onClick={() => setActive("langgraph")}>
                  <strong>I have an app</strong>
                  <span>Copy SDK or LangGraph code and keep your invoke flow.</span>
                </button>
                <button type="button" onClick={() => setActive("chains")}>
                  <strong>I need fallbacks</strong>
                  <span>Build provider-aware routes and override them per call.</span>
                </button>
              </div>

              <div className="docs-grid">
                <div className="docs-panel">
                  <h3>First request</h3>
                  <p>
                    Use the base URL from the browser where the Console is open. Local installs show
                    localhost; production shows the production origin.
                  </p>
                </div>
                <DocsCodeBlock title="Environment" language="shell" code={snippets.env} />
              </div>
              <DocsCodeBlock title="Chat Completions" language="shell" code={snippets.firstCurl} />
              <DocsCodeBlock title="Embeddings for RAG" language="shell" code={snippets.embeddings} />
            </>
          )}

          {active === "setup" && (
            <>
              <SectionHeading number="02" title="Setup from zero">
                This is the normal operator flow. It keeps secrets in the Console and gives applications only a Gozar API key.
              </SectionHeading>
              <ol className="docs-steps">
                <li>
                  <AccountsIcon size={20} />
                  <div>
                    <strong>Connect upstream accounts</strong>
                    <span>Add a subscription credential or provider API key. Secrets are stored server-side.</span>
                  </div>
                  <Link to={ROUTES.accounts}>Accounts</Link>
                </li>
                <li>
                  <RefreshIcon size={20} />
                  <div>
                    <strong>Refresh model catalogs</strong>
                    <span>Gozar reads live provider models when the provider exposes a model-list API.</span>
                  </div>
                  <Link to={ROUTES.dashboard}>Dashboard</Link>
                </li>
                <li>
                  <ChainIcon size={20} />
                  <div>
                    <strong>Create a chain</strong>
                    <span>Configure the LLM path and an optional Embeddings path.</span>
                  </div>
                  <Link to={ROUTES.chains}>Chains</Link>
                </li>
                <li>
                  <TokenIcon size={20} />
                  <div>
                    <strong>Create a Gozar API key</strong>
                    <span>Pin the default chain. Revealing the key does not rotate it.</span>
                  </div>
                  <Link to={ROUTES.tokens}>API keys</Link>
                </li>
              </ol>
              <div className="docs-grid">
                <div className="docs-panel">
                  <h3>What the app needs</h3>
                  <ul className="docs-list">
                    <li><code>GOZAR_BASE_URL</code> ending in <code>/v1</code>.</li>
                    <li><code>GOZAR_API_KEY</code> created in the API keys page.</li>
                    <li><code>GOZAR_MODEL</code> selected from <code>GET /v1/models</code>.</li>
                    <li><code>GOZAR_EMBEDDING_MODEL</code> from an OpenAI or OpenRouter account.</li>
                    <li>Optional <code>GOZAR_CHAIN_ID</code> for per-call overrides.</li>
                  </ul>
                </div>
                <div className="docs-panel">
                  <h3>What stays in Gozar</h3>
                  <ul className="docs-list">
                    <li>OpenAI, OpenRouter, Codex, or cloud provider credentials.</li>
                    <li>Refresh and re-authentication handling for subscription accounts.</li>
                    <li>Fallback policy and model health alerts.</li>
                    <li>Trace, usage, and analytics history.</li>
                  </ul>
                </div>
              </div>
              <DocsCodeBlock title="Discover available models" language="shell" code={snippets.models} />
            </>
          )}

          {active === "chains" && (
            <>
              <SectionHeading number="03" title="Two request paths, one chain">
                The request endpoint selects the LLM or Embeddings path automatically. The API key keeps one chain ID.
              </SectionHeading>
              <div className="docs-callout">
                <AlertIcon size={20} />
                <p>
                  LLM and embedding models are provider-scoped. Every node stores its own model ID,
                  so each fallback can use a different provider without changing application code.
                </p>
              </div>

              <div className="docs-grid">
                <div className="docs-panel">
                  <h3>LLM path</h3>
                  <p>
                    Used only by <code>POST /v1/chat/completions</code>. A subscription can be
                    primary and an API-key provider can use its own chat model as fallback.
                  </p>
                </div>
                <div className="docs-panel">
                  <h3>Embeddings path</h3>
                  <p>
                    Used only by <code>POST /v1/embeddings</code>. Add OpenAI or OpenRouter nodes
                    and select the exact embedding model for each provider.
                  </p>
                </div>
              </div>

              <h3>Routing precedence</h3>
              <ol className="docs-precedence">
                <li><span>1</span><div><strong>Per-call override</strong><p>Header or <code>gozar.chain_id</code>.</p></div></li>
                <li><span>2</span><div><strong>API key default</strong><p>The chain pinned to the selected Gozar API key.</p></div></li>
                <li><span>3</span><div><strong>Model selector</strong><p>Legacy route matching for model-selector chains.</p></div></li>
                <li><span>4</span><div><strong>Catch-all</strong><p>The first enabled chain without a selector.</p></div></li>
              </ol>

              <h3>Stable, idempotent chain creation</h3>
              <p>
                Automation should upsert by a caller-owned key. If the same chain already exists,
                this endpoint returns and updates that chain instead of creating duplicates.
              </p>
              <DocsCodeBlock title="Upsert by stable key" language="shell" code={snippets.upsert} />

              <div className="docs-grid">
                <div>
                  <h3>Override by header</h3>
                  <DocsCodeBlock title="One request, one chain" language="shell" code={snippets.overrideHeader} />
                </div>
                <div>
                  <h3>Override by body</h3>
                  <DocsCodeBlock title="SDK extra body payload" language="http" code={snippets.overrideBody} />
                </div>
              </div>
            </>
          )}

          {active === "langgraph" && (
            <>
              <SectionHeading number="04" title="Use Gozar inside real LLM apps">
                The application still calls the normal SDK or llm.invoke. Gozar handles route selection below it.
              </SectionHeading>

              <div className="docs-example-picker" role="tablist" aria-label="Integration examples">
                {examples.map((example) => (
                  <button
                    key={example.id}
                    type="button"
                    role="tab"
                    aria-selected={selected.id === example.id}
                    className={
                      selected.id === example.id
                        ? "docs-example-picker__item docs-example-picker__item--active"
                        : "docs-example-picker__item"
                    }
                    onClick={() => setSelectedExample(example.id)}
                  >
                    <strong>{example.label}</strong>
                    <span>{example.summary}</span>
                  </button>
                ))}
              </div>

              <DocsCodeBlock title={selected.title} language={selected.language} code={selected.code} />

              <div className="docs-callout docs-callout--info">
                <ExternalLinkIcon size={20} />
                <p>
                  Chat calls use the selected chain's LLM path. Embeddings use its separate
                  Embeddings path and accept OpenAI or OpenRouter API-key accounts. In LangChain Python, keep
                  <code> use_responses_api=False</code> when ChatOpenAI must use Chat Completions.
                </p>
              </div>
            </>
          )}

          {active === "api" && (
            <>
              <SectionHeading number="05" title="API reference">
                App traffic uses a Gozar API key. Operator control endpoints use the Console access token.
              </SectionHeading>
              <div className="docs-api-table-wrap">
                <table className="docs-api-table">
                  <thead><tr><th>Surface</th><th>Endpoint</th><th>Auth</th><th>Use</th></tr></thead>
                  <tbody>
                    <tr><td>Chat</td><td><code>POST /v1/chat/completions</code></td><td>Gozar API key</td><td>LLM calls from apps.</td></tr>
                    <tr><td>Embeddings</td><td><code>POST /v1/embeddings</code></td><td>Gozar API key</td><td>Real vectors for RAG, search, and memory.</td></tr>
                    <tr><td>Models</td><td><code>GET /v1/models</code></td><td>Gozar API key</td><td>Models reachable by that API key route.</td></tr>
                    <tr><td>Chains</td><td><code>GET|POST /api/chains</code></td><td>Operator token</td><td>Create and edit visual fallback chains.</td></tr>
                    <tr><td>Stable chain</td><td><code>PUT /api/chains/by-key/:key</code></td><td>Operator token</td><td>Idempotent chain automation.</td></tr>
                    <tr><td>Route test</td><td><code>POST /api/tokens/:id/test</code></td><td>Operator token</td><td>Test the selected key without pasting its secret.</td></tr>
                    <tr><td>Trace detail</td><td><code>GET /api/traces/:id</code></td><td>Operator token</td><td>Inspect every routing attempt using the response trace ID.</td></tr>
                  </tbody>
                </table>
              </div>
              <div className="docs-grid">
                <DocsCodeBlock title="Model list" language="shell" code={snippets.models} />
                <DocsCodeBlock title="Embeddings" language="shell" code={snippets.embeddings} />
              </div>
              <DocsCodeBlock title="Console route test" language="shell" code={snippets.routeTest} />
              <p className="docs-footnote">
                The <code>/v1</code> base URL is for app traffic. The <code>/api</code> surface is
                operator control-plane traffic and should not be embedded in end-user apps.
              </p>
            </>
          )}

          {active === "ops" && (
            <>
              <SectionHeading number="06" title="Operations and debugging">
                Use the Dashboard for health, API keys for route tests, Traces for request evidence, and Analytics for usage.
              </SectionHeading>
              <div className="docs-grid docs-grid--three">
                <div className="docs-panel">
                  <RefreshIcon size={20} />
                  <h3>Model drift</h3>
                  <p>
                    If a provider removes a model, chain health marks the affected node so the
                    operator can replace it before traffic breaks.
                  </p>
                </div>
                <div className="docs-panel">
                  <TokenIcon size={20} />
                  <h3>API key stability</h3>
                  <p>
                    A Gozar API key stays stable until the operator rotates or revokes it. Upstream
                    subscription refresh happens behind that interface.
                  </p>
                </div>
                <div className="docs-panel">
                  <TracesIcon size={20} />
                  <h3>Trace evidence</h3>
                  <p>
                    Every completed request records its chain, node attempts, provider, effective
                    model, sanitized errors, fallback decisions, usage, and elapsed time.
                  </p>
                </div>
              </div>

              <h3>Compatible routing metadata</h3>
              <div className="docs-panel docs-panel--split">
                <div>
                  <p>
                    Normal responses keep the OpenAI Chat Completions or Embeddings shape unchanged. Read
                    <code> x-request-id</code> or <code>x-gozar-trace-id</code> from the HTTP
                    response and inspect that request in Traces.
                    The <code>x-gozar-route</code> header reports <code>chat</code> or
                    <code> embeddings</code>.
                  </p>
                  <p>
                    Raw HTTP clients can opt in to a top-level <code>gozar</code> extension.
                    LangChain and LangGraph calls do not need it and continue using their standard
                    <code> response_metadata</code> and <code>usage_metadata</code>.
                  </p>
                </div>
                <DocsCodeBlock title="Opt-in metadata" language="shell" code={snippets.traceMetadata} />
              </div>

              <h3>When a request fails</h3>
              <ol className="docs-checklist">
                <li>Open the API key row and run the built-in route test.</li>
                <li>Open the latest Trace and check the selected provider, model, and upstream error.</li>
                <li>Check Dashboard model health for missing or removed models.</li>
                <li>Reconnect or refresh the upstream account if the provider reports auth failure.</li>
                <li>Move the failed node lower in the chain or replace it with a healthy provider model.</li>
              </ol>
            </>
          )}
        </article>
      </div>
    </div>
  );
}
