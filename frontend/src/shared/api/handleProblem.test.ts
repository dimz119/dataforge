import { describe, expect, it } from 'vitest';

import {
  DEFAULT_RATE_LIMIT_SECONDS,
  isRetryable,
  resolveProblem,
} from './handleProblem';
import { ApiError } from './problem';

function problem(slug: string, extra: Partial<ConstructorParameters<typeof ApiError>[0]> = {}) {
  return new ApiError({
    status: 400,
    type: `https://docs.dataforge.dev/problems/${slug}`,
    title: `${slug} title`,
    ...extra,
  });
}

describe('resolveProblem — the §10.1 problem-type switch', () => {
  it('maps permission-denied and not-found to the same page-not-found surface (cross-ws masking)', () => {
    expect(resolveProblem(problem('permission-denied')).kind).toBe('page-not-found');
    expect(resolveProblem(problem('not-found')).kind).toBe('page-not-found');
  });

  it('routes validation-error to the form-pointer surface', () => {
    const action = resolveProblem(problem('validation-error'));
    expect(action.kind).toBe('form');
  });

  it('routes manifest-validation-failed (MAN-V*) to the overlay surface', () => {
    const action = resolveProblem(problem('manifest-validation-failed'));
    expect(action.kind).toBe('overlay');
  });

  it('routes conflict / invalid-state-transition / idempotency-key-conflict to a toast with detail', () => {
    for (const slug of ['conflict', 'invalid-state-transition', 'idempotency-key-conflict']) {
      const action = resolveProblem(problem(slug, { detail: 'slug taken' }));
      expect(action).toMatchObject({ kind: 'toast', detail: 'slug taken' });
    }
  });

  it('routes quota-exceeded to the quota banner with detail', () => {
    const action = resolveProblem(problem('quota-exceeded', { detail: 'aggregate TPS cap' }));
    expect(action).toMatchObject({ kind: 'quota', detail: 'aggregate TPS cap' });
  });

  it('routes cursor-expired to the teaching-moment callout', () => {
    expect(resolveProblem(problem('cursor-expired')).kind).toBe('cursor-expired');
  });

  it('routes rate-limited with the server retryAfter', () => {
    const action = resolveProblem(problem('rate-limited', { retryAfter: 12 }));
    expect(action).toMatchObject({ kind: 'rate-limited', retryAfter: 12 });
  });

  it('falls back to the default countdown when rate-limited omits retryAfter', () => {
    const action = resolveProblem(problem('rate-limited'));
    expect(action).toMatchObject({ kind: 'rate-limited', retryAfter: DEFAULT_RATE_LIMIT_SECONDS });
  });

  it('keeps authentication-required as a middleware concern', () => {
    expect(resolveProblem(problem('authentication-required')).kind).toBe('auth');
  });

  it('collapses unknown slugs to the generic ErrorState surface', () => {
    const action = resolveProblem(problem('some-future-problem'));
    expect(action.kind).toBe('generic');
  });

  it('collapses non-ApiError throwables to a generic ApiError', () => {
    const action = resolveProblem(new Error('boom'));
    expect(action.kind).toBe('generic');
    if (action.kind === 'generic') {
      expect(action.error).toBeInstanceOf(ApiError);
      expect(action.error.detail).toBe('boom');
    }
  });
});

describe('isRetryable', () => {
  it('is true for network and 5xx', () => {
    expect(isRetryable(new ApiError({ status: 0, type: 'about:blank', title: 'net' }))).toBe(true);
    expect(isRetryable(new ApiError({ status: 503, type: 'about:blank', title: '5xx' }))).toBe(true);
  });

  it('is false for deterministic 4xx and non-ApiError', () => {
    expect(isRetryable(problem('validation-error'))).toBe(false);
    expect(isRetryable(new Error('x'))).toBe(false);
  });
});
