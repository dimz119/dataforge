import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { tokenManager } from '../api/client';
import { makeFakeFactory } from './fakeSocket';
import type { EventFrame, ReadyFrame, ResumeAckFrame } from './frames';
import { useStreamTail } from './useStreamTail';

const ready: ReadyFrame = {
  type: 'ready',
  protocol: 'dataforge.events.v1',
  stream_id: 's1',
  position: { cursor: 'c0' },
  filters: {},
};

function evt(cursor: string, type = 'order_placed'): EventFrame {
  return { type: 'event', cursor, event: { event_type: type, cursor } };
}

describe('useStreamTail', () => {
  beforeEach(() => {
    // The hook's socket asks the shared TokenManager for a JWT; stub it.
    vi.spyOn(tokenManager, 'getValidAccessToken').mockResolvedValue('jwt-test');
    vi.spyOn(tokenManager, 'refresh').mockResolvedValue('jwt-test');
  });
  afterEach(() => vi.restoreAllMocks());

  it('authenticates on connect and opens after ready', async () => {
    const fake = makeFakeFactory();
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory }),
    );
    // Let getValidAccessToken resolve and the transport get built.
    await waitFor(() => expect(fake.sockets.length).toBe(1));

    act(() => {
      fake.last().open(); // → client sends auth frame
    });
    expect(fake.last().authFrame()).toMatchObject({ type: 'auth', access_token: 'jwt-test' });

    act(() => {
      fake.last().emit(ready);
    });
    await waitFor(() => expect(result.current.status).toBe('open'));
  });

  it('counts events and exposes the resume cursor', async () => {
    const fake = makeFakeFactory();
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory }),
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
      for (let i = 0; i < 5; i++) fake.last().emit(evt(`c${i}`));
    });
    await waitFor(() => expect(result.current.counters.received).toBe(5));
    expect(result.current.lastCursor).toBe('c4');
  });

  it('records a resume_ack behind gap and a drop_notice as notices', async () => {
    const fake = makeFakeFactory();
    const ack: ResumeAckFrame = {
      type: 'resume_ack',
      position: { cursor: 'c10' },
      behind: { events: 100, from_cursor: 'c1' },
    };
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory }),
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
      fake.last().emit(ack);
      fake.last().emit({ type: 'drop_notice', dropped: 7, resume_cursor: 'c12' });
    });
    await waitFor(() => {
      expect(result.current.notices.some((n) => n.kind === 'catching-up')).toBe(true);
      expect(result.current.notices.some((n) => n.kind === 'drop')).toBe(true);
      expect(result.current.counters.droppedByServer).toBe(7);
    });
  });

  it('samples the display above the 200 EPS threshold while keeping counters exact', async () => {
    const fake = makeFakeFactory();
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory, bufferSize: 2_000 }),
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
      for (let i = 0; i < 1_000; i++) fake.last().emit(evt(`c${i}`));
    });
    await waitFor(() => expect(result.current.counters.received).toBe(1_000));
    expect(result.current.sampling.active).toBe(true);
    expect(result.current.counters.displayed).toBeLessThan(1_000);
    expect(result.current.events.length).toBeLessThan(1_000);
  });

  it('clear() empties the display and resets counters', async () => {
    const fake = makeFakeFactory();
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory }),
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
      for (let i = 0; i < 3; i++) fake.last().emit(evt(`c${i}`));
    });
    await waitFor(() => expect(result.current.counters.received).toBe(3));
    act(() => result.current.clear());
    await waitFor(() => {
      expect(result.current.counters.received).toBe(0);
      expect(result.current.events.length).toBe(0);
    });
  });

  it('REST gap-fills a behind resume_ack and appends recovered events', async () => {
    const fake = makeFakeFactory();
    const getSpy = vi.spyOn(api, 'GET').mockResolvedValue({
      data: { data: [{ event_type: 'recovered', cursor: 'g1' }], next_cursor: 'g1' },
      error: undefined,
    } as never);
    const ack: ResumeAckFrame = {
      type: 'resume_ack',
      position: { cursor: 'c10' },
      behind: { events: 5, from_cursor: 'g0' },
    };
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory }),
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
      fake.last().emit(ack);
    });
    await waitFor(() => {
      expect(getSpy).toHaveBeenCalled();
      expect(result.current.counters.received).toBeGreaterThan(0);
    });
  });

  it('reports reconnecting status on an abnormal close', async () => {
    const fake = makeFakeFactory();
    const { result } = renderHook(() =>
      useStreamTail('s1', { transportFactory: fake.factory }),
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
    });
    await waitFor(() => expect(result.current.status).toBe('open'));
    act(() => fake.last().serverClose(1006));
    await waitFor(() => expect(result.current.status).toBe('reconnecting'));
  });
});
