import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createApiClient, tokenManager } from './client';
import { ApiError } from './problem';
import { TokenManager, type AccessGrant } from './token';

const base = 'https://docs.dataforge.dev/problems';

/** Build a JWT whose exp is far in the future so it is never "expiring soon". */
function liveJwt(): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `h.${btoa(JSON.stringify({ exp }))}.s`;
}

function jsonResponse(status: number, body: unknown, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function makeTm(refresh: () => Promise<AccessGrant>): TokenManager {
  return new TokenManager({ refreshFn: refresh, channel: null });
}

const okBody = { user_id: 'u1', email: 'a@b.c', is_verified: true, created_at: 'x', memberships: [] };

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal('fetch', fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('client auth middleware (§6.3)', () => {
  it('injects the Bearer header from a held, non-expiring token', async () => {
    const tm = makeTm(() => Promise.reject(new Error('should not refresh')));
    tm.setAccess({ access_token: liveJwt(), access_expires_in: 3600 });
    fetchSpy.mockResolvedValueOnce(jsonResponse(200, okBody));
    const client = createApiClient(tm);

    const { data, error } = await client.GET('/api/v1/users/me');
    expect(error).toBeUndefined();
    expect(data).toMatchObject({ user_id: 'u1' });
    const req = fetchSpy.mock.calls[0][0] as Request;
    expect(req.headers.get('Authorization')).toMatch(/^Bearer h\./);
  });

  it('proactively single-flight refreshes when no token is held', async () => {
    const access = liveJwt();
    const refresh = vi.fn(() => Promise.resolve({ access_token: access, access_expires_in: 3600 }));
    const tm = makeTm(refresh);
    fetchSpy.mockResolvedValueOnce(jsonResponse(200, okBody));
    const client = createApiClient(tm);

    await client.GET('/api/v1/users/me');
    expect(refresh).toHaveBeenCalledTimes(1);
    const req = fetchSpy.mock.calls[0][0] as Request;
    expect(req.headers.get('Authorization')).toBe(`Bearer ${access}`);
  });

  it('does NOT attach a Bearer header to unauthenticated endpoints', async () => {
    const tm = makeTm(() => Promise.reject(new Error('no refresh on login')));
    fetchSpy.mockResolvedValueOnce(jsonResponse(200, { access_token: liveJwt(), access_expires_in: 900 }));
    const client = createApiClient(tm);

    await client.POST('/api/v1/auth/login', { body: { email: 'a@b.c', password: 'pw' } });
    const req = fetchSpy.mock.calls[0][0] as Request;
    expect(req.headers.has('Authorization')).toBe(false);
  });

  it('reactively refreshes once on a 401 and retries the original request exactly once', async () => {
    const first = liveJwt();
    const second = liveJwt();
    const tm = makeTm(() => Promise.resolve({ access_token: second, access_expires_in: 3600 }));
    tm.setAccess({ access_token: first, access_expires_in: 3600 });

    // 1st call (proxied via openapi-fetch) → 401; retry (raw fetch) → 200.
    fetchSpy
      .mockResolvedValueOnce(
        jsonResponse(401, { type: `${base}/authentication-required`, title: 'Unauthorized' }),
      )
      .mockResolvedValueOnce(jsonResponse(200, okBody));
    const client = createApiClient(tm);

    const { data, error } = await client.GET('/api/v1/users/me');
    expect(error).toBeUndefined();
    expect(data).toMatchObject({ user_id: 'u1' });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const retry = fetchSpy.mock.calls[1][0] as Request;
    expect(retry.headers.get('Authorization')).toBe(`Bearer ${second}`);
    expect(retry.headers.get('X-Df-Retried')).toBe('1');
  });

  it('throws a typed ApiError when the refresh itself fails on a 401', async () => {
    const tm = makeTm(() => Promise.reject(new Error('refresh dead')));
    tm.setAccess({ access_token: liveJwt(), access_expires_in: 3600 });
    fetchSpy.mockResolvedValueOnce(
      jsonResponse(401, { type: `${base}/authentication-required`, title: 'Unauthorized' }),
    );
    const client = createApiClient(tm);

    // A thrown middleware error propagates as a rejection (openapi-fetch contract).
    const err = await client.GET('/api/v1/users/me').then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
  });

  it('parses a non-401 problem body into a typed ApiError', async () => {
    const tm = makeTm(() => Promise.resolve({ access_token: liveJwt(), access_expires_in: 3600 }));
    fetchSpy.mockResolvedValueOnce(
      jsonResponse(429, { type: `${base}/rate-limited`, title: 'Too many', retry_after_seconds: 5 }),
    );
    const client = createApiClient(tm);

    const err = await client.GET('/api/v1/users/me').then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).slug).toBe('rate-limited');
    expect((err as ApiError).retryAfter).toBe(5);
  });

  it('normalizes a transport failure to an ApiError(status 0)', async () => {
    const tm = makeTm(() => Promise.resolve({ access_token: liveJwt(), access_expires_in: 3600 }));
    fetchSpy.mockRejectedValueOnce(new TypeError('connection refused'));
    const client = createApiClient(tm);

    const err = await client.GET('/api/v1/users/me').then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
    expect((err as ApiError).isNetworkError).toBe(true);
  });
});

describe('production refresh transport (§6.2)', () => {
  afterEach(() => {
    tokenManager.clear();
  });

  it('refreshes via the df_refresh cookie path (credentials: include)', async () => {
    const access = liveJwt();
    fetchSpy.mockResolvedValueOnce(jsonResponse(200, { access_token: access, access_expires_in: 900 }));
    const token = await tokenManager.refresh();
    expect(token).toBe(access);
    const req = fetchSpy.mock.calls[0][0] as string;
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(req).toContain('/api/v1/auth/refresh');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
  });

  it('throws a typed ApiError when the refresh cookie is missing/expired (401)', async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResponse(401, { type: `${base}/authentication-required`, title: 'Unauthorized' }),
    );
    await expect(tokenManager.refresh()).rejects.toBeInstanceOf(ApiError);
  });

  it('surfaces a transport failure during refresh as a network ApiError', async () => {
    fetchSpy.mockRejectedValueOnce(new TypeError('offline'));
    const err = await tokenManager.refresh().then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  });
});
