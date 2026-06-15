import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { makeFakeFactory } from './fakeSocket';
import type { ServerFrame } from './frames';
import { TailSocket, type TailSocketHandlers, type TailStatus } from './socket';

const readyFrame: ServerFrame = {
  type: 'ready',
  protocol: 'dataforge.events.v1',
  stream_id: 's1',
  position: { cursor: 'c0' },
  filters: {},
};

/** Flush pending microtasks (resolve getAccessToken/refresh) WITHOUT firing timers. */
async function flushMicro(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function harness(overrides: Partial<TailSocketHandlers> = {}) {
  const statuses: TailStatus[] = [];
  const frames: ServerFrame[] = [];
  const terminals: number[] = [];
  const handlers: TailSocketHandlers = {
    onStatus: (s) => statuses.push(s),
    onFrame: (f) => frames.push(f),
    onTerminal: (c) => terminals.push(c),
    ...overrides,
  };
  return { statuses, frames, terminals, handlers };
}

describe('TailSocket', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('sends an auth frame with the JWT and cursor on open, opens on ready', async () => {
    const fake = makeFakeFactory();
    const { statuses, handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt-abc'),
      refreshToken: () => Promise.resolve('jwt-new'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.setCursor('cursor-99');
    socket.connect();
    await flushMicro(); // resolve getAccessToken without firing the 10 s auth timer

    fake.last().open(); // onopen → client sends auth
    const auth = fake.last().authFrame();
    expect(auth).toMatchObject({ type: 'auth', access_token: 'jwt-abc', cursor: 'cursor-99' });

    fake.last().emit(readyFrame);
    expect(statuses).toContain('open');
    expect(socket.getCursor()).toBe('cursor-99'); // ready does not overwrite a known cursor
    socket.close();
  });

  it('reconnects with backoff after an abnormal close', async () => {
    const fake = makeFakeFactory();
    const { statuses, handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
      random: () => 1, // full jitter → exactly the max delay
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame);

    fake.last().serverClose(1006); // abnormal close
    expect(statuses).toContain('reconnecting');

    // First backoff = min(1000*2^0, 30000) * jitter(1) = 1000 ms.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(fake.sockets.length).toBe(2); // a new connection was opened
    socket.close();
  });

  it('refreshes the token then reconnects on a 4401 close', async () => {
    const fake = makeFakeFactory();
    // Model TokenManager: refresh rotates the token getAccessToken then returns.
    let token = 'jwt-old';
    const refresh = vi.fn(() => {
      token = 'jwt-refreshed';
      return Promise.resolve(token);
    });
    const { handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve(token),
      refreshToken: refresh,
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame);

    fake.last().serverClose(4401); // auth failed mid-connection
    await vi.advanceTimersByTimeAsync(1); // drain refresh → reconnect microtask chain
    await flushMicro();
    expect(refresh).toHaveBeenCalledOnce();
    expect(fake.sockets.length).toBe(2);
    fake.last().open();
    expect(fake.last().authFrame()).toMatchObject({ access_token: 'jwt-refreshed' });
    socket.close();
  });

  it('treats 4404 as terminal — no reconnect', async () => {
    const fake = makeFakeFactory();
    const { terminals, handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().serverClose(4404);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(terminals).toEqual([4404]);
    expect(fake.sockets.length).toBe(1); // never reconnected
  });

  it('reconnects immediately (no backoff) on a 1001 going-away close', async () => {
    const fake = makeFakeFactory();
    const { handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame);
    fake.last().serverClose(1001);
    await flushMicro(); // immediate reconnect, no timer
    expect(fake.sockets.length).toBe(2);
    socket.close();
  });

  it('aborts and reconnects when the server selects a wrong subprotocol', async () => {
    const fake = makeFakeFactory({ protocol: 'wrong.protocol' });
    const { statuses, handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
      random: () => 0,
    });
    socket.connect();
    await flushMicro();
    fake.last().open(); // wrong protocol → no auth sent, abort
    expect(fake.last().sent).toHaveLength(0);
    expect(statuses).toContain('reconnecting');
    socket.close();
  });

  it('heartbeat resets the liveness watchdog (no reconnect before 45 s of silence)', async () => {
    const fake = makeFakeFactory();
    const { handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame);
    await vi.advanceTimersByTimeAsync(30_000);
    fake.last().emit({
      type: 'heartbeat',
      server_time: 't',
      last_cursor: 'c1',
      delivered: 1,
      dropped: 0,
    });
    await vi.advanceTimersByTimeAsync(30_000); // 60 s total, but heartbeat reset at 30 s
    expect(fake.sockets.length).toBe(1); // still alive
    socket.close();
  });

  it('close() is terminal — no reconnect after an explicit user close', async () => {
    const fake = makeFakeFactory();
    const { statuses, handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame);
    socket.close();
    expect(statuses[statuses.length - 1]).toBe('closed');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fake.sockets.length).toBe(1);
  });

  it('seeds the cursor from the ready frame when none was set', async () => {
    const fake = makeFakeFactory();
    const { handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame); // ready.position.cursor = 'c0'
    expect(socket.getCursor()).toBe('c0');
    fake.last().emit({ type: 'event', cursor: 'c1', event: {} });
    expect(socket.getCursor()).toBe('c1'); // advances on each event
    socket.close();
  });

  it('ignores malformed and non-frame messages', async () => {
    const fake = makeFakeFactory();
    const frames: ServerFrame[] = [];
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers: { onStatus: () => {}, onFrame: (f) => frames.push(f), onTerminal: () => {} },
      transportFactory: fake.factory,
      wsBase: 'ws://test',
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emitRaw('not json');
    fake.last().emitRaw(JSON.stringify({ type: 'bogus' }));
    expect(frames).toHaveLength(0);
    socket.close();
  });

  it('reconnects after the 10 s auth deadline when no ready arrives', async () => {
    const fake = makeFakeFactory();
    const { handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
      random: () => 0,
    });
    socket.connect();
    await flushMicro();
    fake.last().open(); // auth sent, but no ready frame
    await vi.advanceTimersByTimeAsync(10_000); // auth deadline
    await vi.advanceTimersByTimeAsync(10); // 0 ms backoff
    await flushMicro();
    expect(fake.sockets.length).toBe(2);
    socket.close();
  });

  it('fires the watchdog after 45 s of silence and reconnects', async () => {
    const fake = makeFakeFactory();
    const { handlers } = harness();
    const socket = new TailSocket({
      streamId: 's1',
      getAccessToken: () => Promise.resolve('jwt'),
      refreshToken: () => Promise.resolve('jwt'),
      handlers,
      transportFactory: fake.factory,
      wsBase: 'ws://test',
      random: () => 0, // zero jitter → reconnect fires immediately after watchdog
    });
    socket.connect();
    await flushMicro();
    fake.last().open();
    fake.last().emit(readyFrame);

    await vi.advanceTimersByTimeAsync(45_000); // no frames → watchdog → reconnect(0 ms)
    await vi.advanceTimersByTimeAsync(10); // fire the 0 ms reconnect timer
    await flushMicro(); // resolve the reconnect's getAccessToken
    expect(fake.sockets.length).toBe(2);
    socket.close();
  });
});
