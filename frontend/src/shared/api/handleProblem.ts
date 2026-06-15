/**
 * Central RFC 9457 problem-type switch (frontend-architecture §10.1).
 *
 * The §10.1 behavior table is encoded ONCE here: every handler switches on the
 * problem `type` SUFFIX (the slug), never on status alone. Pages delegate their
 * `onError` to {@link resolveProblem} and render the returned `ProblemAction`
 * with the surfaces they own (form pointers, OverlayErrorMap, toasts, the
 * NotFoundPage, a rate-limit countdown). This keeps the convention DRY and
 * unit-testable; the per-surface rendering stays local to each feature.
 *
 * The full problem-type catalog is owned by api-specification §2.7.1.
 */
import { ApiError } from './problem';

/**
 * The console-side classification of a problem (the §10.1 right column). Pages
 * pattern-match on `kind` and render the corresponding surface.
 */
export type ProblemAction =
  /** `permission-denied` / `not-found` → render NotFoundPage (cross-ws probes look identical). */
  | { kind: 'page-not-found' }
  /** `validation-error` → map `errors[].pointer` onto form fields (§10.4). */
  | { kind: 'form'; error: ApiError }
  /** `manifest-validation-failed` (MAN-V*) → OverlayErrorMap control highlight (§9.4). */
  | { kind: 'overlay'; error: ApiError }
  /** `conflict` → toast with `detail` (e.g. slug taken). */
  | { kind: 'toast'; title: string; detail?: string }
  /** `quota-exceeded` → QuotaBanner on the acting page (Phase 11 adds the upgrade CTA). */
  | { kind: 'quota'; detail?: string }
  /** `cursor-expired` → tail notice / REST callout (a teaching moment). */
  | { kind: 'cursor-expired'; detail?: string }
  /** `rate-limited` → toast + acting button disabled for `retryAfter` seconds. */
  | { kind: 'rate-limited'; retryAfter: number; detail?: string }
  /** `authentication-required` → handled in middleware (§6.3); should never reach a page. */
  | { kind: 'auth' }
  /** anything else / unparseable / network → generic ErrorState with request id. */
  | { kind: 'generic'; error: ApiError };

/** Default seconds to disable an action when a `rate-limited` problem omits `retryAfter`. */
export const DEFAULT_RATE_LIMIT_SECONDS = 30;

/**
 * Classify an error into the §10.1 action. Non-ApiError throwables (render
 * crashes, programmer errors) collapse to `generic` so the caller always has a
 * renderable surface.
 */
export function resolveProblem(error: unknown): ProblemAction {
  if (!(error instanceof ApiError)) {
    return {
      kind: 'generic',
      error: new ApiError({
        status: 0,
        type: 'about:blank',
        title: error instanceof Error ? 'Something went wrong' : 'Unexpected error',
        detail: error instanceof Error ? error.message : undefined,
      }),
    };
  }
  switch (error.slug) {
    case 'permission-denied':
    case 'not-found':
      return { kind: 'page-not-found' };
    case 'validation-error':
      return { kind: 'form', error };
    case 'manifest-validation-failed':
      return { kind: 'overlay', error };
    case 'conflict':
    case 'invalid-state-transition':
    case 'idempotency-key-conflict':
      return { kind: 'toast', title: error.title, detail: error.detail };
    case 'quota-exceeded':
      return { kind: 'quota', detail: error.detail };
    case 'cursor-expired':
      return { kind: 'cursor-expired', detail: error.detail };
    case 'rate-limited':
      return {
        kind: 'rate-limited',
        retryAfter:
          typeof error.retryAfter === 'number' && error.retryAfter > 0
            ? error.retryAfter
            : DEFAULT_RATE_LIMIT_SECONDS,
        detail: error.detail,
      };
    case 'authentication-required':
      return { kind: 'auth' };
    default:
      return { kind: 'generic', error };
  }
}

/**
 * True when a problem is a transport/5xx/transient failure worth a retry button
 * (the generic ErrorState's "Try again"). Validation/permission/conflict are
 * deterministic — retrying is pointless, so callers hide the button for those.
 */
export function isRetryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.isNetworkError) return true;
  return error.status >= 500;
}
