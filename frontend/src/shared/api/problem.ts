/**
 * RFC 9457 problem-details error surface (frontend-architecture §10.1).
 *
 * Phase 1 ships the typed `ApiError` shape so the QueryClient retry policy
 * (frontend-architecture §4.1) is real from day one. Phase 7 replaces this
 * module's parsing surface with the full problem-details parser wired into the
 * generated openapi-fetch client (`shared/api/client.ts`).
 */

/** One entry of a `validation-error` problem's `errors[]` array. */
export interface ProblemFieldError {
  /** JSON Pointer to the offending request field. */
  pointer: string;
  detail: string;
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

  constructor(args: {
    status: number;
    type: string;
    title: string;
    detail?: string;
    errors?: readonly ProblemFieldError[];
    retryAfter?: number;
  }) {
    super(`${String(args.status)} ${args.title}`);
    this.name = 'ApiError';
    this.status = args.status;
    this.type = args.type;
    this.title = args.title;
    this.detail = args.detail;
    this.errors = args.errors;
    this.retryAfter = args.retryAfter;
  }
}
