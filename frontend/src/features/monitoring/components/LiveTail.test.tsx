import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { tokenManager } from '../../../shared/api/client';
import { ToastProvider } from '../../../shared/ui';
import { makeFakeFactory } from '../../../shared/ws';
import type { EventFrame, ReadyFrame } from '../../../shared/ws';
import { LiveTail } from './LiveTail';

const ready: ReadyFrame = {
  type: 'ready',
  protocol: 'dataforge.events.v1',
  stream_id: 's1',
  position: { cursor: 'c0' },
  filters: {},
};

function evt(i: number): EventFrame {
  return {
    type: 'event',
    cursor: `c${i}`,
    event: { event_type: 'order_placed', sequence_no: i, occurred_at: '2026-06-14T10:00:00Z' },
  };
}

// jsdom reports 0 layout sizes; give the scroll container a real viewport so the
// virtualizer windows the list (otherwise it would render everything or nothing).
function stubLayout(): void {
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, value: 448 });
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 600 });
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, value: 40_000 });
  Object.defineProperty(HTMLElement.prototype, 'scrollTop', { configurable: true, writable: true, value: 0 });
}

describe('LiveTail virtualization (Phase 7 exit criterion)', () => {
  beforeEach(() => {
    vi.spyOn(tokenManager, 'getValidAccessToken').mockResolvedValue('jwt-test');
    vi.spyOn(tokenManager, 'refresh').mockResolvedValue('jwt-test');
    stubLayout();
  });
  afterEach(() => vi.restoreAllMocks());

  it('renders a BOUNDED number of DOM rows under a 1000-event flood', async () => {
    const fake = makeFakeFactory();
    render(
      <ToastProvider>
        <LiveTail
          streamId="s1"
          streamStatus="running"
          knownTypes={['order_placed']}
          transportFactory={fake.factory}
        />
      </ToastProvider>,
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));

    act(() => {
      fake.last().open();
      fake.last().emit(ready);
      // A flood well above the 200 EPS sampling threshold and the 1000-row buffer.
      for (let i = 0; i < 1_000; i++) fake.last().emit(evt(i));
    });

    // After the 4 Hz flush, rows render — but the virtualizer windows them: the live
    // DOM row count must stay far below the flood (no freeze, no 1000 nodes).
    await waitFor(
      () => {
        const rows = screen.queryAllByRole('button', { expanded: false });
        expect(rows.length).toBeGreaterThan(0);
        expect(rows.length).toBeLessThan(100);
      },
      { timeout: 2_000 },
    );
  });
});
