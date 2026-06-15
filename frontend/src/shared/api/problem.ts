/**
 * RFC 9457 problem-details error surface (frontend-architecture §10.1).
 *
 * The full problem-type catalog is owned by api-specification §2.7.1. Handlers
 * switch on the problem `type` SUFFIX (the slug), never on status alone (§10.1).
 * `parseProblem` turns any non-2xx body into a typed `ApiError`; it is wired into
 * the openapi-fetch client middleware (`shared/api/client.ts`, §6.3).
 */

/**
 * Closed problem-type catalog for the MVP (api-specification §2.7.1). Additions
 * are additive; unknown slugs are tolerated and surface via the generic
 * `ErrorState` (§10.1, last row).
 */
export type ProblemSlug =
  | 'validation-error'
  | 'cursor-invalid'
  | 'ambiguous-credentials'
  | 'authentication-required'
  | 'authentication-failed'
  | 'invalid-api-key'
  | 'email-not-verified'
  | 'permission-denied'
  | 'quota-exceeded'
  | 'not-found'
  | 'conflict'
  | 'invalid-state-transition'
  | 'idempotency-key-conflict'
  | 'cursor-expired'
  | 'payload-too-large'
  | 'manifest-validation-failed'
  | 'rate-limited'
  | 'internal-error'
  | 'service-unavailable';

/**
 * One entry of an `errors[]` array. The backend uses `{field, code, message}`
 * for `validation-error` and `{code, path, message}` for `conflict` /
 * `manifest-validation-failed` (MAN-*). We normalize both to a single shape:
 * `pointer` is the JSON Pointer (`path`) or a `#/`-prefixed `field`.
 */
export interface ProblemFieldError {
  /** JSON Pointer to the offending request field or config path. */
  pointer: string;
  detail: string;
  /** Machine code (e.g. `max_value`, `MAN-V201`) when present. */
  code?: string;
  /** Numeric bound/actual for MAN-* overlay errors (§9.4 OverlayErrorMap). */
  bound?: number;
  actual?: number;
}

/** Typed RFC 9457 problem document raised by every non-2xx API response. */
export class ApiError extends Error {
  /** HTTP status; 0 means the request never reached the server (network error). */
  readonly status: number;
  /** Problem `type` URI; handlers switch on its suffix, never on status alone. */
  readonly type: string;
  readonly title: string;
  readonly detail?: string;
  readonly errors?: readonly ProblemFieldError[];
  /** Seconds to wait, from `rate-limited` problems. */
  readonly retryAfter?: number;

  /** `request_id` extension member — the support handle (§10.1, generic row). */
  readonly requestId?: string;

  constructor(args: {
    status: number;
    type: string;
    title: string;
    detail?: string;
    errors?: readonly ProblemFieldError[];
    retryAfter?: number;
    requestId?: string;
  }) {
    super(`${String(args.status)} ${args.title}`);
    this.name = 'ApiError';
    this.status = args.status;
    this.type = args.type;
    this.title = args.title;
    this.detail = args.detail;
    this.errors = args.errors;
    this.retryAfter = args.retryAfter;
    this.requestId = args.requestId;
  }

  /**
   * The problem-type SUFFIX, e.g. `validation-error` — the value handlers switch
   * on (§10.1). Derived from the `type` URI's last path segment.
   */
  get slug(): string {
    const last = this.type.split('/').filter(Boolean).pop();
    return last ?? this.type;
  }

  /** True when this is a network/transport failure (the request never landed). */
  get isNetworkError(): boolean {
    return this.status === 0;
  }
}

/** Raw RFC 9457 body shape as it arrives over the wire (snake_case extensions). */
interface RawProblem {
  type?: unknown;
  title?: unknown;
  status?: unknown;
  detail?: unknown;
  request_id?: unknown;
  retry_after_seconds?: unknown;
  errors?: unknown;
}

function normalizeErrors(raw: unknown): ProblemFieldError[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: ProblemFieldError[] = [];
  for (const item of raw) {
    if (item == null || typeof item !== 'object') continue;
    const e = item as Record<string, unknown>;
    // `validation-error` → {field, code, message}; `conflict`/MAN-* → {code, path, message}.
    const path = e['path'];
    const field = e['field'];
    const pointer =
      typeof path === 'string' ? path : typeof field === 'string' ? `#/${field}` : '';
    const message = e['message'];
    const code = e['code'];
    const bound = e['bound'];
    const actual = e['actual'];
    out.push({
      pointer,
      detail: typeof message === 'string' ? message : '',
      code: typeof code === 'string' ? code : undefined,
      bound: typeof bound === 'number' ? bound : undefined,
      actual: typeof actual === 'number' ? actual : undefined,
    });
  }
  return out.length > 0 ? out : undefined;
}

/**
 * Parse any non-2xx response into a typed `ApiError`. Tolerant of non-conforming
 * bodies (HTML 502s, empty 204-on-error): falls back to a synthetic problem so
 * the generic `ErrorState` always has something to render (§10.1, last row).
 *
 * @param status  HTTP status (0 for a transport failure)
 * @param body    parsed JSON body, or `undefined`/string when unparseable
 * @param headers response headers (for the `Retry-After` fallback)
 */
export function parseProblem(
  status: number,
  body: unknown,
  headers?: Headers,
): ApiError {
  const p = (body && typeof body === 'object' ? body : {}) as RawProblem;
  const headerRetry = headers?.get('Retry-After');
  const retryAfter =
    typeof p.retry_after_seconds === 'number'
      ? p.retry_after_seconds
      : headerRetry != null && headerRetry !== ''
        ? Number.parseInt(headerRetry, 10)
        : undefined;

  return new ApiError({
    status,
    type:
      typeof p.type === 'string'
        ? p.type
        : status === 0
          ? 'https://docs.dataforge.dev/problems/network-error'
          : 'about:blank',
    title:
      typeof p.title === 'string'
        ? p.title
        : status === 0
          ? 'Network error'
          : `Request failed (${String(status)})`,
    detail: typeof p.detail === 'string' ? p.detail : undefined,
    errors: normalizeErrors(p.errors),
    retryAfter: Number.isNaN(retryAfter) ? undefined : retryAfter,
    requestId: typeof p.request_id === 'string' ? p.request_id : undefined,
  });
}

/** A network/transport failure (`fetch` rejected before any response). */
export function networkError(cause?: unknown): ApiError {
  return parseProblem(0, {
    detail: cause instanceof Error ? cause.message : undefined,
  });
}
