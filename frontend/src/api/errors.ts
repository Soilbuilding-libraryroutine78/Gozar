import type { ApiErrorDetail, ApiErrorEnvelope } from "./types";

/**
 * A typed error raised by the API client for any non-2xx response or transport
 * failure. It preserves the backend error envelope (code/message/details) so the
 * UI can render a precise, secret-free message and branch on the status code.
 */
export class ApiError extends Error {
  /** HTTP status code, or 0 for a transport/network failure. */
  readonly status: number;
  /** Stable machine-readable code from the envelope (e.g. "VALIDATION_ERROR"). */
  readonly code: string;
  /** Structured per-field details, when the backend supplied them. */
  readonly details: ReadonlyArray<ApiErrorDetail>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: ReadonlyArray<ApiErrorDetail> = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** True when the failure is an authentication error (missing/expired session). */
  get isAuthError(): boolean {
    return this.status === 401;
  }
}

/** Narrowing guard for the admin error envelope shape. */
function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as { error?: unknown };
  if (typeof candidate.error !== "object" || candidate.error === null) {
    return false;
  }
  const inner = candidate.error as { code?: unknown; message?: unknown };
  return typeof inner.code === "string" && typeof inner.message === "string";
}

/**
 * Build an {@link ApiError} from a failed HTTP response, parsing the backend
 * error envelope when present and falling back to the status text otherwise.
 */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON or empty body; fall back to the status line below.
  }

  if (isErrorEnvelope(body)) {
    const details = Array.isArray(body.error.details) ? body.error.details : [];
    return new ApiError(response.status, body.error.code, body.error.message, details);
  }

  return new ApiError(
    response.status,
    "HTTP_ERROR",
    response.statusText || `Request failed with status ${response.status}`,
  );
}
