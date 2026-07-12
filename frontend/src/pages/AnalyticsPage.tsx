import { useCallback, useEffect, useMemo, useState } from "react";

import { listAccounts } from "../api/accounts";
import {
  accountReport,
  systemReport,
  tokenReport,
  type AnalyticsRange,
} from "../api/analytics";
import { listTokens } from "../api/tokens";
import { ApiError } from "../api/errors";
import {
  AlertIcon,
  AnalyticsIcon,
  GaugeIcon,
  InboxIcon,
  RefreshIcon,
  TokenIcon,
} from "../components/icons";
import { PageGuide } from "../components/PageGuide";
import { Spinner } from "../components/Spinner";
import type {
  AccountAnalyticsResponse,
  AccountResponse,
  SystemAnalyticsResponse,
  TokenAnalyticsResponse,
  TokenResponse,
} from "../api/types";
import { formatNumber } from "./accounts/format";
import { providerLabel } from "./accounts/providers";
import { RangeSelector } from "./analytics/RangeSelector";
import {
  defaultRangeInputs,
  describeConsumptionReport,
  describeRange,
  formatRate,
} from "./analytics/format";

/** The three analytics scopes (Requirements 15.1, 15.2, 15.3). */
type Scope = "system" | "token" | "account";

/** A fetched report, tagged by the scope it was produced for. */
type Report =
  | { readonly kind: "system"; readonly data: SystemAnalyticsResponse }
  | { readonly kind: "token"; readonly data: TokenAnalyticsResponse }
  | { readonly kind: "account"; readonly data: AccountAnalyticsResponse };

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

const SCOPES: ReadonlyArray<{ id: Scope; label: string }> = [
  { id: "system", label: "System" },
  { id: "token", label: "API key" },
  { id: "account", label: "Account" },
];

/**
 * Analytics view (Requirements 15.1, 15.2, 15.3, 17.4): per-token, per-account,
 * and system reports over an operator-selected time range. A scope selector picks
 * the report; token and account scopes pick a subject from the connected
 * tokens/accounts; the range selector drives the half-open `[start, end)` window
 * sent to the Analytics_Service.
 *
 * Every async surface -- the subject lists and each report -- renders explicit
 * loading, empty, and error states. Reports carry no secrets. Icons are outline
 * SVGs; there are no emoji.
 */
export function AnalyticsPage(): JSX.Element {
  const initialRange = useMemo(defaultRangeInputs, []);
  const [startInput, setStartInput] = useState(initialRange.start);
  const [endInput, setEndInput] = useState(initialRange.end);
  const [range, setRange] = useState<AnalyticsRange | null>(null);

  const [scope, setScope] = useState<Scope>("system");
  const [tokenId, setTokenId] = useState<string>("");
  const [accountId, setAccountId] = useState<string>("");

  // Subjects for the token/account pickers.
  const [tokens, setTokens] = useState<ReadonlyArray<TokenResponse>>([]);
  const [accounts, setAccounts] = useState<ReadonlyArray<AccountResponse>>([]);
  const [subjectsLoading, setSubjectsLoading] = useState(true);
  const [subjectsError, setSubjectsError] = useState<string | null>(null);

  const [report, setReport] = useState<Report | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  // Apply the default range once so the system report loads on first view.
  useEffect(() => {
    const start = new Date(initialRange.start);
    const end = new Date(initialRange.end);
    setRange({ start: start.toISOString(), end: end.toISOString() });
  }, [initialRange]);

  const loadSubjects = useCallback(async (): Promise<void> => {
    setSubjectsLoading(true);
    setSubjectsError(null);
    try {
      const [tokenResult, accountResult] = await Promise.all([
        listTokens(),
        listAccounts(),
      ]);
      setTokens(tokenResult);
      setAccounts(accountResult);
    } catch (cause) {
      setSubjectsError(messageFor(cause));
    } finally {
      setSubjectsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSubjects();
  }, [loadSubjects]);

  const fetchReport = useCallback(
    async (current: AnalyticsRange): Promise<void> => {
      setReportError(null);
      // Token/account scopes need a chosen subject before a request makes sense.
      if (scope === "token" && tokenId === "") {
        setReport(null);
        return;
      }
      if (scope === "account" && accountId === "") {
        setReport(null);
        return;
      }
      setReportLoading(true);
      try {
        if (scope === "system") {
          setReport({ kind: "system", data: await systemReport(current) });
        } else if (scope === "token") {
          setReport({ kind: "token", data: await tokenReport(tokenId, current) });
        } else {
          setReport({ kind: "account", data: await accountReport(accountId, current) });
        }
      } catch (cause) {
        setReportError(messageFor(cause));
        setReport(null);
      } finally {
        setReportLoading(false);
      }
    },
    [scope, tokenId, accountId],
  );

  // Refetch whenever the applied range or the selection changes.
  useEffect(() => {
    if (range !== null) {
      void fetchReport(range);
    }
  }, [range, fetchReport]);

  return (
    <>
      <div className="toolbar">
        <p className="toolbar__lead">
          Review usage and consumption per API key, per account, or across the system.
        </p>
      </div>

      <PageGuide
        id="analytics-guide-title"
        title="Usage review"
        description="Analytics aggregates redacted usage records over a selected time range, with system, API key, and account views for capacity and troubleshooting."
        steps={[
          {
            title: "Pick a range",
            description: "Reports use a half-open time window so repeated checks stay consistent.",
            Icon: GaugeIcon,
          },
          {
            title: "Choose a scope",
            description: "Switch between system totals, a single API key, or an upstream account.",
            Icon: AnalyticsIcon,
          },
          {
            title: "Act on signals",
            description: "Use errors, success rate, and token averages to tune limits and chains.",
            Icon: TokenIcon,
          },
        ]}
      />

      <RangeSelector
        startInput={startInput}
        endInput={endInput}
        onStartChange={setStartInput}
        onEndChange={setEndInput}
        onApply={setRange}
      />

      <div className="analytics-controls">
        <div className="segmented" role="tablist" aria-label="Report scope">
          {SCOPES.map((option) => (
            <button
              key={option.id}
              type="button"
              role="tab"
              aria-selected={scope === option.id}
              className={
                scope === option.id
                  ? "segmented__option segmented__option--active"
                  : "segmented__option"
              }
              onClick={() => setScope(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>

        {scope === "token" && (
          <SubjectPicker
            label="API key"
            loading={subjectsLoading}
            error={subjectsError}
            emptyHint="No API keys to report on yet."
            onRetry={() => void loadSubjects()}
            value={tokenId}
            onChange={setTokenId}
            options={tokens.map((token) => ({
              value: token.token_id,
              label: token.label,
            }))}
          />
        )}

        {scope === "account" && (
          <SubjectPicker
            label="Account"
            loading={subjectsLoading}
            error={subjectsError}
            emptyHint="No accounts connected to report on yet."
            onRetry={() => void loadSubjects()}
            value={accountId}
            onChange={setAccountId}
            options={accounts.map((account) => ({
              value: account.account_id,
              label: `${account.label} (${providerLabel(account.provider)})`,
            }))}
          />
        )}
      </div>

      <ReportBody
        scope={scope}
        report={report}
        loading={reportLoading}
        error={reportError}
        tokenSelected={tokenId !== ""}
        accountSelected={accountId !== ""}
        onRetry={() => {
          if (range !== null) {
            void fetchReport(range);
          }
        }}
      />
    </>
  );
}

/** A labelled subject dropdown with its own loading / error / empty states. */
function SubjectPicker({
  label,
  loading,
  error,
  emptyHint,
  onRetry,
  value,
  onChange,
  options,
}: {
  readonly label: string;
  readonly loading: boolean;
  readonly error: string | null;
  readonly emptyHint: string;
  readonly onRetry: () => void;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly options: ReadonlyArray<{ value: string; label: string }>;
}): JSX.Element {
  if (loading) {
    return (
      <div className="state state--loading" role="status">
        <Spinner label={`Loading ${label.toLowerCase()}s`} size={18} />
        <span>Loading {label.toLowerCase()}s...</span>
      </div>
    );
  }

  if (error !== null) {
    return (
      <div className="state state--error" role="alert">
        <AlertIcon size={18} aria-hidden />
        <p>{error}</p>
        <button type="button" className="button button--ghost" onClick={onRetry}>
          <RefreshIcon size={18} aria-hidden />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  if (options.length === 0) {
    return (
      <div className="state state--empty">
        <InboxIcon size={24} aria-hidden />
        <p>{emptyHint}</p>
      </div>
    );
  }

  const selectId = `analytics-subject-${label.toLowerCase()}`;
  return (
    <div className="field analytics-subject">
      <label htmlFor={selectId}>{label}</label>
      <select id={selectId} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Select a {label.toLowerCase()}...</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

/** Renders the loading / error / awaiting-selection / populated states of a report. */
function ReportBody({
  scope,
  report,
  loading,
  error,
  tokenSelected,
  accountSelected,
  onRetry,
}: {
  readonly scope: Scope;
  readonly report: Report | null;
  readonly loading: boolean;
  readonly error: string | null;
  readonly tokenSelected: boolean;
  readonly accountSelected: boolean;
  readonly onRetry: () => void;
}): JSX.Element {
  if (scope === "token" && !tokenSelected) {
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>Select an API key to view its analytics.</p>
      </div>
    );
  }

  if (scope === "account" && !accountSelected) {
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>Select an account to view its analytics.</p>
      </div>
    );
  }

  if (loading && report === null) {
    return (
      <div className="state state--loading" role="status">
        <Spinner label="Loading report" size={22} />
        <span>Loading report...</span>
      </div>
    );
  }

  if (error !== null && report === null) {
    return (
      <div className="state state--error" role="alert">
        <AlertIcon size={22} aria-hidden />
        <p>{error}</p>
        <button type="button" className="button button--ghost" onClick={onRetry}>
          <RefreshIcon size={18} aria-hidden />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  if (report === null) {
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>No report to display.</p>
      </div>
    );
  }

  return <ReportView report={report} />;
}

/** A single labelled metric tile. */
function Metric({ label, value }: { readonly label: string; readonly value: string }): JSX.Element {
  return (
    <div className="metric">
      <span className="metric__label">{label}</span>
      <span className="metric__value">{value}</span>
    </div>
  );
}

function ReportHeader({
  title,
  description,
  range,
}: {
  readonly title: string;
  readonly description: string;
  readonly range: AnalyticsRange;
}): JSX.Element {
  return (
    <div className="analytics-report__head">
      <div>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      <span>{describeRange(range)}</span>
    </div>
  );
}

/** Renders the populated report for the active scope. */
function ReportView({ report }: { readonly report: Report }): JSX.Element {
  if (report.kind === "system") {
    const data = report.data;
    const successful = Math.max(0, data.request_count - data.error_count);
    return (
      <section className="analytics-report">
        <ReportHeader
          title="System overview"
          description="All routed requests handled by Gozar in this range."
          range={data.range}
        />
        <div className="metric-grid">
          <Metric label="Requests" value={formatNumber(data.request_count)} />
          <Metric label="Successful requests" value={formatNumber(successful)} />
          <Metric label="Errors" value={formatNumber(data.error_count)} />
          <Metric label="Error rate" value={formatRate(data.error_rate)} />
          <Metric
            label="Avg tokens / request"
            value={formatAverage(data.total_tokens, data.request_count)}
          />
          <Metric label="Total tokens" value={formatNumber(data.total_tokens)} />
        </div>
      </section>
    );
  }

  if (report.kind === "token") {
    const data = report.data;
    return (
      <section className="analytics-report">
        <ReportHeader
          title="API key report"
          description="Usage produced by the selected API key."
          range={data.range}
        />
        <div className="metric-grid">
          <Metric label="Requests" value={formatNumber(data.counts.request_count)} />
          <Metric
            label="Avg tokens / request"
            value={formatAverage(data.counts.total_tokens, data.counts.request_count)}
          />
          <Metric label="Prompt tokens" value={formatNumber(data.counts.prompt_tokens)} />
          <Metric label="Completion tokens" value={formatNumber(data.counts.completion_tokens)} />
          <Metric
            label="Prompt share"
            value={formatShare(data.counts.prompt_tokens, data.counts.total_tokens)}
          />
          <Metric label="Total tokens" value={formatNumber(data.counts.total_tokens)} />
        </div>
        <dl className="detail-list analytics-report__consumption">
          <div className="detail-list__row">
            <dt>Consumption</dt>
            <dd>{describeConsumptionReport(data.consumption)}</dd>
          </div>
        </dl>
      </section>
    );
  }

  const data = report.data;
  const accountSuccessRate = formatSuccessRate(
    data.counts.request_count,
    data.error_count,
  );
  return (
    <section className="analytics-report">
      <ReportHeader
        title="Account report"
        description="Traffic routed through the selected upstream credential."
        range={data.range}
      />
      <div className="metric-grid">
        <Metric label="Requests" value={formatNumber(data.counts.request_count)} />
        <Metric label="Errors" value={formatNumber(data.error_count)} />
        <Metric label="Success rate" value={accountSuccessRate} />
        <Metric
          label="Avg tokens / request"
          value={formatAverage(data.counts.total_tokens, data.counts.request_count)}
        />
        <Metric label="Prompt tokens" value={formatNumber(data.counts.prompt_tokens)} />
        <Metric label="Completion tokens" value={formatNumber(data.counts.completion_tokens)} />
        <Metric label="Total tokens" value={formatNumber(data.counts.total_tokens)} />
      </div>
      <dl className="detail-list analytics-report__consumption">
        <div className="detail-list__row">
          <dt>Consumption</dt>
          <dd>{describeConsumptionReport(data.consumption)}</dd>
        </div>
      </dl>
    </section>
  );
}

function formatAverage(total: number, count: number): string {
  if (count <= 0) {
    return "0";
  }
  return formatNumber(total / count);
}

function formatShare(part: number, total: number): string {
  if (total <= 0) {
    return "0.0%";
  }
  return formatRate(part / total);
}

function formatSuccessRate(requests: number, errors: number): string {
  if (requests <= 0) {
    return "0.0%";
  }
  const successful = Math.max(0, requests - errors);
  return formatRate(successful / requests);
}
