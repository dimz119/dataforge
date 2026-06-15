import { describe, expect, it, vi } from 'vitest';

import { decodeJwtExpMs, TokenManager, type AccessGrant, type AuthChannel } from './token';

/** Build a fake JWT whose `exp` claim is `expSeconds`. */
function jwt(expSeconds: number): string {
  const payload = btoa(JSON.stringify({ exp: expSeconds }));
  return `h.${payload}.s`;
}

function makeManager(refresh: () => Promise<AccessGrant>, now = () => 1_000_000) {
  // channel: null disables BroadcastChannel so tests are isolated.
  return new TokenManager({ refreshFn: refresh, channel: null, now });
}

describe('decodeJwtExpMs', () => {
  it('decodes the exp claim to ms', () => {
    expect(decodeJwtExpMs(jwt(1700))).toBe(1700 * 1000);
  });
  it('returns undefined for a non-JWT string', () => {
    expect(decodeJwtExpMs('not-a-jwt')).toBeUndefined();
  });
});

describe('TokenManager', () => {
  it('single-flights concurrent refreshes into ONE network call', async () => {
    let calls = 0;
    let resolveRefresh!: (g: AccessGrant) => void;
    const refresh = vi.fn(() => {
      calls += 1;
      return new Promise<AccessGrant>((resolve) => {
        resolveRefresh = resolve;
      });
    });
    const tm = makeManager(refresh);

    // 5 concurrent callers while no token is held → all share one in-flight promise.
    const promises = Array.from({ length: 5 }, () => tm.getValidAccessToken());
    expect(calls).toBe(1);

    resolveRefresh({ access_token: jwt(2000), access_expires_in: 900 });
    const results = await Promise.all(promises);
    expect(results.every((r) => r === jwt(2000))).toBe(true);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('returns the cached token without refreshing when not expiring soon', async () => {
    const now = () => 1_000_000;
    const refresh = vi.fn(() =>
      Promise.resolve({ access_token: jwt(2000), access_expires_in: 900 }),
    );
    const tm = makeManager(refresh, now);
    // exp = 2000s → 2_000_000ms, now = 1_000_000ms → ~1000s remaining > 30s skew.
    tm.setAccess({ access_token: jwt(2000), access_expires_in: 900 });
    const token = await tm.getValidAccessToken();
    expect(token).toBe(jwt(2000));
    expect(refresh).not.toHaveBeenCalled();
  });

  it('refreshes proactively when the token expires within the 30s skew', async () => {
    const now = () => 1_990_000; // 10s before exp=2000s
    const refresh = vi.fn(() =>
      Promise.resolve({ access_token: jwt(3000), access_expires_in: 900 }),
    );
    const tm = makeManager(refresh, now);
    tm.setAccess({ access_token: jwt(2000), access_expires_in: 900 });
    expect(tm.isExpiringSoon()).toBe(true);
    const token = await tm.getValidAccessToken();
    expect(token).toBe(jwt(3000));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('clears the token when refresh fails and rethrows', async () => {
    const refresh = vi.fn(() => Promise.reject(new Error('refresh rejected')));
    const tm = makeManager(refresh);
    await expect(tm.refresh()).rejects.toThrow('refresh rejected');
    expect(tm.getAccessToken()).toBeNull();
  });

  it('starts a fresh single-flight after the previous one settles', async () => {
    const refresh = vi.fn(() =>
      Promise.resolve({ access_token: jwt(2000), access_expires_in: 900 }),
    );
    const tm = makeManager(refresh);
    await tm.refresh();
    tm.clear();
    await tm.refresh();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('multi-tab logout: a broadcast logout clears the token and notifies listeners', () => {
    // A fake channel the manager will listen on; capture its message handler.
    type Listener = Parameters<AuthChannel['addEventListener']>[1];
    let handler: Listener | null = null;
    const channel: AuthChannel = {
      postMessage: vi.fn(),
      addEventListener: (_t, l) => {
        handler = l;
      },
      close: vi.fn(),
    };
    const refresh = vi.fn(() =>
      Promise.resolve({ access_token: jwt(2000), access_expires_in: 900 }),
    );
    const tm = new TokenManager({ refreshFn: refresh, channel, now: () => 1_000_000 });
    tm.setAccess({ access_token: jwt(2000), access_expires_in: 900 });
    const onLogout = vi.fn();
    tm.onLogout(onLogout);

    (handler as Listener | null)?.({ data: { type: 'logout' } });
    expect(tm.getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it('broadcastLogout clears locally and posts a logout to siblings', () => {
    const postMessage = vi.fn();
    const channel: AuthChannel = { postMessage, addEventListener: vi.fn(), close: vi.fn() };
    const tm = new TokenManager({ refreshFn: vi.fn(), channel, now: () => 1_000_000 });
    tm.setAccess({ access_token: jwt(2000), access_expires_in: 900 });
    tm.broadcastLogout();
    expect(tm.getAccessToken()).toBeNull();
    expect(postMessage).toHaveBeenCalledWith({ type: 'logout' });
  });

  it('adopts a sibling tab token broadcast without refreshing, and unsubscribes listeners', () => {
    type Listener = Parameters<AuthChannel['addEventListener']>[1];
    let handler: Listener | null = null;
    const channel: AuthChannel = {
      postMessage: vi.fn(),
      addEventListener: (_t, l) => {
        handler = l;
      },
      close: vi.fn(),
    };
    const refresh = vi.fn();
    const tm = new TokenManager({ refreshFn: refresh, channel, now: () => 1_000_000 });
    const onLogout = vi.fn();
    const unsub = tm.onLogout(onLogout);

    (handler as Listener | null)?.({ data: { type: 'token', access: jwt(9000), exp: 9_000_000 } });
    expect(tm.getAccessToken()).toBe(jwt(9000));
    expect(refresh).not.toHaveBeenCalled();

    unsub();
    (handler as Listener | null)?.({ data: { type: 'logout' } });
    expect(onLogout).not.toHaveBeenCalled();
  });

  it('dispose closes the channel and drops listeners', () => {
    const close = vi.fn();
    const channel: AuthChannel = { postMessage: vi.fn(), addEventListener: vi.fn(), close };
    const tm = new TokenManager({ refreshFn: vi.fn(), channel });
    tm.dispose();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('uses a real BroadcastChannel by default when the platform supports it', () => {
    // jsdom provides BroadcastChannel; the default-channel branch should wire it.
    const tm = new TokenManager({ refreshFn: vi.fn() });
    expect(() => {
      tm.broadcastLogout();
    }).not.toThrow();
    tm.dispose();
  });
});
