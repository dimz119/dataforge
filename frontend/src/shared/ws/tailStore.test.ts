import { describe, expect, it } from 'vitest';

import type { DropNoticeFrame, EventFrame, ResumeAckFrame } from './frames';
import { TailStore } from './tailStore';

function eventFrame(cursor: string, type = 'order_placed'): EventFrame {
  return { type: 'event', cursor, event: { event_type: type, cursor } };
}

describe('TailStore', () => {
  it('counts every event exactly and displays all below the sampling threshold', () => {
    const store = new TailStore({ now: () => 0 });
    for (let i = 0; i < 50; i++) store.ingest(eventFrame(`c${i}`));
    store.flush();
    const snap = store.getSnapshot();
    expect(snap.counters.received).toBe(50);
    expect(snap.counters.displayed).toBe(50);
    expect(snap.sampling.active).toBe(false);
    expect(snap.events.length).toBe(50);
    expect(snap.lastCursor).toBe('c49');
  });

  it('samples the DISPLAY but keeps counters exact above 200 EPS', () => {
    let t = 0;
    const store = new TailStore({ now: () => t });
    // 1000 events in the same 250 ms bucket → EPS ~ 4000 → factor 20.
    for (let i = 0; i < 1_000; i++) store.ingest(eventFrame(`c${i}`));
    store.flush();
    const snap = store.getSnapshot();
    expect(snap.counters.received).toBe(1_000); // exact
    expect(snap.sampling.active).toBe(true);
    expect(snap.counters.displayed).toBeLessThan(1_000); // display sampled
    expect(snap.counters.displayed + snap.counters.sampledOut).toBe(1_000);
    t = 1; // silence the linter on the unused-let; keeps now() callable
    expect(t).toBe(1);
  });

  it('records a drop_notice as a counter + inline notice', () => {
    const store = new TailStore({ now: () => 0 });
    const drop: DropNoticeFrame = { type: 'drop_notice', dropped: 250, resume_cursor: 'c10' };
    store.ingest(drop);
    store.flush();
    const snap = store.getSnapshot();
    expect(snap.counters.droppedByServer).toBe(250);
    expect(snap.notices.some((n) => n.kind === 'drop')).toBe(true);
  });

  it('surfaces a "catching up" notice from a behind resume_ack', () => {
    const store = new TailStore({ now: () => 0 });
    const ack: ResumeAckFrame = {
      type: 'resume_ack',
      position: { cursor: 'c5' },
      behind: { events: 2864, from_cursor: 'c1' },
    };
    store.ingest(ack);
    store.flush();
    expect(store.getSnapshot().notices.some((n) => n.kind === 'catching-up')).toBe(true);
  });

  it('surfaces a cursor-expired teaching notice without closing', () => {
    const store = new TailStore({ now: () => 0 });
    store.ingest({
      type: 'error',
      problem: { type: 'https://docs.dataforge.dev/problems/cursor-expired' },
    });
    store.flush();
    expect(store.getSnapshot().notices.some((n) => n.kind === 'cursor-expired')).toBe(true);
  });

  it('respects a defensive client-side type filter', () => {
    const store = new TailStore({ now: () => 0, eventTypes: ['order_placed'] });
    store.ingest(eventFrame('c1', 'order_placed'));
    store.ingest(eventFrame('c2', 'page_view')); // filtered out
    store.flush();
    expect(store.getSnapshot().counters.received).toBe(1);
  });

  it('seeds lastCursor from ready, tracks it, and exposes getLastCursor', () => {
    const store = new TailStore({ now: () => 0 });
    store.ingest({
      type: 'ready',
      protocol: 'dataforge.events.v1',
      stream_id: 's1',
      position: { cursor: 'c0' },
      filters: {},
    });
    expect(store.getLastCursor()).toBe('c0');
    store.ingest(eventFrame('c5'));
    expect(store.getLastCursor()).toBe('c5');
  });

  it('noteReconnect adds an inline reconnect notice', () => {
    const store = new TailStore({ now: () => 0 });
    store.noteReconnect('reconnected — gap backfilled');
    expect(store.getSnapshot().notices.some((n) => n.kind === 'reconnect')).toBe(true);
  });

  it('ingestGapFill appends recovered REST events to the counters', () => {
    const store = new TailStore({ now: () => 0 });
    store.ingestGapFill([{ event_type: 'a' }, { event_type: 'b' }]);
    store.flush();
    expect(store.getSnapshot().counters.received).toBe(2);
  });

  it('clear resets everything via a single flush', () => {
    const store = new TailStore({ now: () => 0 });
    store.ingest(eventFrame('c1'));
    store.clear();
    const snap = store.getSnapshot();
    expect(snap.counters.received).toBe(0);
    expect(snap.events.length).toBe(0);
  });

  it('freezes the display buffer when paused but keeps counting', () => {
    const store = new TailStore({ now: () => 0 });
    store.ingest(eventFrame('c1'));
    store.setDisplayPaused(true);
    store.ingest(eventFrame('c2'));
    store.flush();
    const snap = store.getSnapshot();
    expect(snap.events.length).toBe(1); // c2 not displayed
    expect(snap.counters.received).toBe(2); // but counted
  });
});
