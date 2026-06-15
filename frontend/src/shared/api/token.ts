/**
 * TokenManager — the client-side token contract (frontend-architecture §6).
 *
 * Storage rules (§6.1): the access token lives in a MODULE VARIABLE (process
 * memory) only — never localStorage/sessionStorage/cookie/Query cache. The
 * rotating refresh token rides the HttpOnly `df_refresh` cookie set by the
 * backend and is never readable here. A full reload loses the access token by
 * design; the §6.2 bootstrap silently restores it via the cookie.
 *
 * This module performs the single-flight refresh (§6.3) and multi-tab
 * coordination (§6.4). It does NOT call `fetch` itself (IMP-4) — the transport
 * is injected so only `shared/api/client.ts` owns the network surface.
 */

/** Result of a successful `/auth/refresh` or `/auth/login`. */
export interface AccessGrant {
  access_token: string;
  /** Seconds until the access token expires (`access_expires_in`). */
  access_expires_in: number;
}

/** Injected transport: performs `POST /api/v1/auth/refresh` (cookie rides along). */
export type RefreshFn = () => Promise<AccessGrant>;

/** Cross-tab message shape on the `df-auth` BroadcastChannel (§6.4). */
type AuthBroadcast =
  | { type: 'login'; access: string; exp: number }
  | { type: 'token'; access: string; exp: number }
  | { type: 'logout' };

/** Proactive-refresh threshold: refresh when < 30 s of access-token life remains. */
const REFRESH_SKEW_MS = 30_000;

/**
 * Decode a JWT's `exp` claim (seconds since epoch) WITHOUT verifying the
 * signature — the client trusts the server; this is only used to schedule
 * proactive refresh (§6.3). Returns `undefined` for a non-decodable token.
 */
export function decodeJwtExpMs(token: string): number | undefined {
  const parts = token.split('.');
  if (parts.length < 2) return undefined;
  try {
    const payload = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = atob(payload);
    const claims = JSON.parse(json) as { exp?: unknown };
    return typeof claims.exp === 'number' ? claims.exp * 1000 : undefined;
  } catch {
    return undefined;
  }
}

export interface TokenManagerOptions {
  refreshFn: RefreshFn;
  /** Injectable for tests; defaults to a real BroadcastChannel when supported. */
  channel?: AuthChannel | null;
  /** Injectable clock for tests. */
  now?: () => number;
}

/** Minimal BroadcastChannel surface (so tests can inject a fake). */
export interface AuthChannel {
  postMessage(msg: AuthBroadcast): void;
  addEventListener(type: 'message', listener: (ev: { data: AuthBroadcast }) => void): void;
  close(): void;
}

/**
 * Single-flight token manager. One instance is created at the composition root
 * and shared by the client middleware and the session bootstrap.
 */
export class TokenManager {
  private accessToken: string | null = null;
  private expMs: number | null = null;
  private inFlight: Promise<string> | null = null;
  private readonly refreshFn: RefreshFn;
  private readonly channel: AuthChannel | null;
  private readonly now: () => number;
  private logoutListeners = new Set<() => void>();

  constructor(opts: TokenManagerOptions) {
    this.refreshFn = opts.refreshFn;
    this.now = opts.now ?? (() => Date.now());
    this.channel = opts.channel === undefined ? createDefaultChannel() : opts.channel;
    this.channel?.addEventListener('message', (ev) => this.onBroadcast(ev.data));
  }

  /** Current in-memory access token, or null when none is held. */
  getAccessToken(): string | null {
    return this.accessToken;
  }

  /** True when no token is held, or it expires within the proactive skew (§6.3). */
  isExpiringSoon(): boolean {
    if (this.accessToken == null || this.expMs == null) return true;
    return this.expMs - this.now() <= REFRESH_SKEW_MS;
  }

  /**
   * Adopt a freshly granted access token (from login or bootstrap) and broadcast
   * it so sibling tabs do not race the rotating cookie (§6.4).
   * @param broadcastType `login` on first auth, `token` on a refresh.
   */
  setAccess(grant: AccessGrant, broadcastType: 'login' | 'token' = 'login'): void {
    this.accessToken = grant.access_token;
    this.expMs =
      decodeJwtExpMs(grant.access_token) ?? this.now() + grant.access_expires_in * 1000;
    this.channel?.postMessage({ type: broadcastType, access: grant.access_token, exp: this.expMs });
  }

  /**
   * Return a valid access token, refreshing if missing/expiring. SINGLE-FLIGHT:
   * concurrent callers share one in-flight refresh promise so N queries never
   * issue N refreshes and trip rotation reuse-detection (§6.3).
   */
  async getValidAccessToken(): Promise<string> {
    if (this.accessToken != null && !this.isExpiringSoon()) return this.accessToken;
    return this.refresh();
  }

  /**
   * Force a single-flight refresh (used by the reactive 401 path, §6.3). Returns
   * the new access token. Rejects (and clears) if the refresh itself fails.
   */
  refresh(): Promise<string> {
    if (this.inFlight != null) return this.inFlight;
    this.inFlight = (async () => {
      try {
        const grant = await this.refreshFn();
        this.setAccess(grant, 'token');
        return grant.access_token;
      } catch (err) {
        this.clear();
        throw err;
      } finally {
        this.inFlight = null;
      }
    })();
    return this.inFlight;
  }

  /** Drop the in-memory token (local only — does NOT broadcast or hit the network). */
  clear(): void {
    this.accessToken = null;
    this.expMs = null;
  }

  /**
   * Local logout: clear the token and broadcast `logout` so every tab logs out
   * (§6.4). The backend cookie-clear + blacklist is performed by the caller's
   * `POST /auth/logout`; this only handles client state + fan-out.
   */
  broadcastLogout(): void {
    this.clear();
    this.channel?.postMessage({ type: 'logout' });
  }

  /** Subscribe to cross-tab logout events; returns an unsubscribe fn. */
  onLogout(listener: () => void): () => void {
    this.logoutListeners.add(listener);
    return () => this.logoutListeners.delete(listener);
  }

  /** Release the BroadcastChannel (composition-root teardown / tests). */
  dispose(): void {
    this.channel?.close();
    this.logoutListeners.clear();
  }

  private onBroadcast(msg: AuthBroadcast): void {
    if (msg.type === 'logout') {
      this.clear();
      for (const l of this.logoutListeners) l();
      return;
    }
    // A sibling tab refreshed/logged in: adopt its token without re-refreshing.
    this.accessToken = msg.access;
    this.expMs = msg.exp;
  }
}

/** Real BroadcastChannel when the platform supports it; null in jsdom/tests. */
function createDefaultChannel(): AuthChannel | null {
  if (typeof BroadcastChannel === 'undefined') return null;
  // BroadcastChannel is structurally compatible with AuthChannel (postMessage /
  // addEventListener('message') / close); the message payload is our union.
  const channel = new BroadcastChannel('df-auth');
  return {
    postMessage: (msg) => {
      channel.postMessage(msg);
    },
    addEventListener: (_type, listener) => {
      channel.addEventListener('message', (ev: MessageEvent<AuthBroadcast>) => {
        listener({ data: ev.data });
      });
    },
    close: () => {
      channel.close();
    },
  };
}
