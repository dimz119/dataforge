/**
 * `TailStore` — the framework-free state engine behind `useStreamTail`
 * (frontend-architecture §7.5/§7.6). It owns the ring buffer, exact counters,
 * sampling, the EPS meter, and the inline notice list, and exposes an immutable
 * snapshot flushed at 4 Hz. `useStreamTail` binds it via `useSyncExternalStore`;
 * keeping it a plain class makes the sampling/counter logic unit-testable without
 * React. The store consumes `ServerFrame`s; `TailSocket` owns the wire.
 */
import type { DeliveredEnvelope, ServerFrame } from './frames';
import {
  DEFAULT_BUFFER_SIZE,
  EpsMeter,
  RingBuffer,
  keeps,
  sampleFactor,
} from './sampling';
import type { TailStatus } from './socket';

/** An inline notice row shown amid the tail (§7.4/§7.6). */
export interface TailNotice {
  id: string;
  kind: 'drop' | 'cursor-expired' | 'reconnect' | 'catching-up';
  message: string;
  at: number;
}

export interface TailCounters {
  received: number;
  displayed: number;
  sampledOut: number;
  droppedByServer: number;
  eps: number;
}

/** Immutable snapshot returned by `getSnapshot` (stable until the next flush). */
export interface TailSnapshot {
  events: ReadonlyArray<DeliveredEnvelope>;
  status: TailStatus;
  counters: TailCounters;
  sampling: { active: boolean; keepRatio: number };
  lastCursor: string | null;
  notices: ReadonlyArray<TailNotice>;
}

export interface TailStoreOptions {
  bufferSize?: number;
  /** Defensive client-side event-type filter (in addition to the server filter). */
  eventTypes?: string[];
  now?: () => number;
}

export class TailStore {
  private readonly buffer: RingBuffer<DeliveredEnvelope>;
  private readonly meter = new EpsMeter();
  private readonly now: () => number;
  private readonly eventTypes: Set<string> | null;

  private status: TailStatus = 'connecting';
  private received = 0;
  private displayed = 0;
  private sampledOut = 0;
  private droppedByServer = 0;
  private factor = 1;
  private arrivalIndex = 0;
  private lastCursor: string | null = null;
  private notices: TailNotice[] = [];
  private displayPaused = false;
  private noticeSeq = 0;

  private snapshot: TailSnapshot;
  private listeners = new Set<() => void>();
  private dirty = false;

  constructor(opts: TailStoreOptions = {}) {
    this.buffer = new RingBuffer<DeliveredEnvelope>(opts.bufferSize ?? DEFAULT_BUFFER_SIZE);
    this.now = opts.now ?? (() => Date.now());
    this.eventTypes = opts.eventTypes?.length ? new Set(opts.eventTypes) : null;
    this.snapshot = this.buildSnapshot();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): TailSnapshot => this.snapshot;

  setDisplayPaused(paused: boolean): void {
    this.displayPaused = paused;
  }

  setStatus(status: TailStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.dirty = true;
    this.flush(); // status changes are rare → flush immediately
  }

  /** Feed one server frame (called by the socket handler). */
  ingest(frame: ServerFrame): void {
    switch (frame.type) {
      case 'event':
        this.onEvent(frame.cursor, frame.event);
        break;
      case 'drop_notice':
        this.droppedByServer += frame.dropped;
        this.pushNotice('drop', `${frame.dropped} events dropped by the server (backpressure)`);
        break;
      case 'resume_ack':
        if (frame.behind != null && frame.behind.events > 0) {
          this.pushNotice('catching-up', `catching up: ${frame.behind.events} events`);
        }
        break;
      case 'error':
        if (frame.problem.type?.endsWith('cursor-expired')) {
          this.pushNotice(
            'cursor-expired',
            'resume point expired — tailing live; events older than retention were not recovered',
          );
        }
        break;
      case 'ready':
        if (this.lastCursor == null) this.lastCursor = frame.position.cursor;
        break;
      case 'heartbeat':
        break;
    }
  }

  /** Append REST-recovered gap-fill events (§7.4) as if they had arrived live. */
  ingestGapFill(events: DeliveredEnvelope[]): void {
    for (const event of events) this.onEvent(null, event);
  }

  private onEvent(cursor: string | null, event: DeliveredEnvelope): void {
    if (cursor != null) this.lastCursor = cursor;
    // Defensive client-side type filter (§7.6) — counters reflect only kept types.
    if (this.eventTypes != null) {
      const type = (event as { event_type?: unknown }).event_type;
      if (typeof type === 'string' && !this.eventTypes.has(type)) return;
    }
    this.received += 1;
    const now = this.now();
    this.meter.add(1, now);
    this.factor = sampleFactor(this.meter.rate(now));

    const idx = this.arrivalIndex++;
    if (keeps(idx, this.factor)) {
      if (!this.displayPaused) this.buffer.push(event);
      this.displayed += 1;
    } else {
      this.sampledOut += 1;
    }
    this.dirty = true;
  }

  private pushNotice(kind: TailNotice['kind'], message: string): void {
    this.notices.push({ id: `n${this.noticeSeq++}`, kind, message, at: this.now() });
    if (this.notices.length > 50) this.notices.splice(0, this.notices.length - 50);
    this.dirty = true;
  }

  /** Inline a reconnect-gap notice (called by the hook on a reconnect handoff). */
  noteReconnect(message: string): void {
    this.pushNotice('reconnect', message);
    this.flush();
  }

  clear(): void {
    this.buffer.clear();
    this.notices = [];
    this.received = 0;
    this.displayed = 0;
    this.sampledOut = 0;
    this.droppedByServer = 0;
    this.arrivalIndex = 0;
    this.factor = 1;
    this.dirty = true;
    this.flush();
  }

  getLastCursor(): string | null {
    return this.lastCursor;
  }

  /** Recompute the snapshot and notify listeners if anything changed (4 Hz cadence). */
  flush(): void {
    // Always recompute EPS so the rate decays even when no events arrive.
    const eps = this.meter.rate(this.now());
    const prevEps = this.snapshot.counters.eps;
    if (!this.dirty && eps === prevEps) return;
    this.dirty = false;
    this.snapshot = this.buildSnapshot(eps);
    for (const l of this.listeners) l();
  }

  private buildSnapshot(eps = 0): TailSnapshot {
    const active = this.factor > 1;
    return {
      events: this.buffer.snapshot(),
      status: this.status,
      counters: {
        received: this.received,
        displayed: this.displayed,
        sampledOut: this.sampledOut,
        droppedByServer: this.droppedByServer,
        eps,
      },
      sampling: { active, keepRatio: active ? 1 / this.factor : 1 },
      lastCursor: this.lastCursor,
      notices: this.notices.slice(),
    };
  }
}
