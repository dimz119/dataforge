import { describe, expect, it } from 'vitest';

import { ApiError, networkError, parseProblem } from './problem';

const base = 'https://docs.dataforge.dev/problems';

describe('parseProblem (RFC 9457 → ApiError)', () => {
  it('maps a validation-error with field errors to the validation slug', () => {
    const err = parseProblem(400, {
      type: `${base}/validation-error`,
      title: 'Request validation failed',
      status: 400,
      detail: '1 invalid parameter.',
      request_id: 'req_1',
      errors: [{ field: 'limit', code: 'max_value', message: 'must be <= 1000' }],
    });
    expect(err).toBeInstanceOf(ApiError);
    expect(err.slug).toBe('validation-error');
    expect(err.status).toBe(400);
    expect(err.requestId).toBe('req_1');
    expect(err.errors?.[0]).toMatchObject({
      pointer: '#/limit',
      detail: 'must be <= 1000',
      code: 'max_value',
    });
  });

  it('normalizes conflict/MAN-* {code, path, message} errors with bound/actual', () => {
    const err = parseProblem(422, {
      type: `${base}/manifest-validation-failed`,
      title: 'Manifest validation failed',
      status: 422,
      errors: [
        {
          code: 'MAN-V201',
          path: '/state_machines/shopping_session/states/checkout_started',
          message: 'sum is 1.15; must be <= 1.0',
          bound: 1.0,
          actual: 1.15,
        },
      ],
    });
    expect(err.slug).toBe('manifest-validation-failed');
    expect(err.errors?.[0]).toMatchObject({
      pointer: '/state_machines/shopping_session/states/checkout_started',
      code: 'MAN-V201',
      bound: 1,
      actual: 1.15,
    });
  });

  it('reads retry_after_seconds from a rate-limited problem', () => {
    const err = parseProblem(429, {
      type: `${base}/rate-limited`,
      title: 'Rate limit exceeded',
      retry_after_seconds: 21,
    });
    expect(err.slug).toBe('rate-limited');
    expect(err.retryAfter).toBe(21);
  });

  it('falls back to the Retry-After header when no extension member', () => {
    const headers = new Headers({ 'Retry-After': '13' });
    const err = parseProblem(429, { type: `${base}/rate-limited`, title: 'x' }, headers);
    expect(err.retryAfter).toBe(13);
  });

  it('parses cursor-expired (the teaching-moment 410)', () => {
    const err = parseProblem(410, {
      type: `${base}/cursor-expired`,
      title: 'Cursor expired',
      detail: 'Resume from earliest_cursor.',
    });
    expect(err.slug).toBe('cursor-expired');
    expect(err.status).toBe(410);
  });

  it('tolerates a non-conforming body (HTML 502) with a synthetic problem', () => {
    const err = parseProblem(502, '<html>Bad Gateway</html>');
    expect(err.type).toBe('about:blank');
    expect(err.title).toBe('Request failed (502)');
  });

  it('represents a transport failure as status 0 / isNetworkError', () => {
    const err = networkError(new Error('connection refused'));
    expect(err.status).toBe(0);
    expect(err.isNetworkError).toBe(true);
    expect(err.slug).toBe('network-error');
  });
});
