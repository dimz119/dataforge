/**
 * The single transport-touching module (frontend-architecture §2.2 IMP-4): the
 * ONLY place allowed to call `fetch`. Wraps openapi-fetch with the auth
 * middleware (§6.3): proactive single-flight refresh, Bearer injection, RFC 9457
 * problem parsing, and reactive 401 retry-exactly-once.
 *
 * The typed client (`api`) is what every feature `api.ts` calls:
 *   `api.GET('/api/v1/streams/{stream_id}', { params: { path: { stream_id } } })`
 * Paths, params, bodies, and responses are all compile-time typed against
 * `schema.gen.ts` (§5.1).
 */
import createClient, { type Middleware } from 'openapi-fetch';

import { ApiError, networkError, parseProblem } from './problem';
import { type AccessGrant, TokenManager } from './token';
import type { ApiPaths } from './types';

/** Base URL for the REST API. The Vite dev proxy maps `/api` → the compose API. */
const BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';

/** Endpoints that must NOT carry a Bearer header or trigger proactive refresh. */
const UNAUTHENTICATED_PATHS = new Set<string>([
  '/api/v1/auth/login',
  '/api/v1/auth/signup',
  '/api/v1/auth/refresh',
  '/api/v1/auth/verify-email',
  '/api/v1/auth/resend-verification',
  '/api/v1/auth/password-reset',
  '/api/v1/auth/password-reset/confirm',
]);

function pathOf(url: string): string {
  try {
    return new URL(url, 'http://x').pathname;
  } catch {
    return url;
  }
}

/**
 * The injected refresh transport handed to the TokenManager. It is the ONE place
 * (besides the client below) issuing a raw `fetch` — necessarily so, because the
 * refresh call must NOT recurse through the auth middleware. The `df_refresh`
 * cookie rides automatically; `credentials: 'include'` ensures the rotated
 * cookie is accepted (§6.2/§6.3).
 */
async function refreshTransport(): Promise<AccessGrant> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/v1/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
  } catch (err) {
    throw networkError(err);
  }
  if (!res.ok) {
    throw parseProblem(res.status, await safeJson(res), res.headers);
  }
  return (await res.json()) as AccessGrant;
}

async function safeJson(res: Response): Promise<unknown> {
  try {
    return await res.clone().json();
  } catch {
    return undefined;
  }
}

/** The shared token manager (single-flight refresh, multi-tab logout — §6). */
export const tokenManager = new TokenManager({ refreshFn: refreshTransport });

/**
 * Auth middleware (§6.3). `onRequest` proactively refreshes and injects Bearer;
 * `onResponse` parses problems and performs the reactive 401 retry-exactly-once.
 */
function createAuthMiddleware(tm: TokenManager): Middleware {
  return {
    async onRequest({ request }) {
      const path = pathOf(request.url);
      if (UNAUTHENTICATED_PATHS.has(path)) return request;
      // credentials so the cookie rides on console (JWT) surfaces too.
      const access = tm.isExpiringSoon()
        ? await tm.getValidAccessToken().catch(() => null)
        : tm.getAccessToken();
      if (access != null) request.headers.set('Authorization', `Bearer ${access}`);
      return request;
    },
    async onResponse({ request, response }) {
      if (response.ok) return response;
      const path = pathOf(request.url);

      // Reactive refresh on a 401 authentication problem: refresh once, retry once.
      if (response.status === 401 && !UNAUTHENTICATED_PATHS.has(path) && !request.headers.has('X-Df-Retried')) {
        let newAccess: string;
        try {
          newAccess = await tm.refresh();
        } catch {
          // Second failure path: a refresh failure means the session is gone.
          throw parseProblem(401, await safeJson(response), response.headers);
        }
        const retry = new Request(request, {
          headers: new Headers(request.headers),
        });
        retry.headers.set('Authorization', `Bearer ${newAccess}`);
        retry.headers.set('X-Df-Retried', '1');
        const retried = await fetch(retry);
        if (!retried.ok) throw parseProblem(retried.status, await safeJson(retried), retried.headers);
        return retried;
      }

      throw parseProblem(response.status, await safeJson(response), response.headers);
    },
    onError({ error }) {
      // Transport failure (fetch rejected): normalize to ApiError(status 0).
      if (error instanceof ApiError) return error;
      return networkError(error);
    },
  };
}

function buildClient(tm: TokenManager) {
  const client = createClient<ApiPaths>({
    baseUrl: BASE_URL,
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  client.use(createAuthMiddleware(tm));
  return client;
}

/** The typed API client used by every feature. */
export const api = buildClient(tokenManager);

/**
 * Factory for tests: a client bound to an injected TokenManager. Production code
 * uses the singleton `api` above.
 */
export function createApiClient(tm: TokenManager): ReturnType<typeof buildClient> {
  return buildClient(tm);
}

export { ApiError } from './problem';
