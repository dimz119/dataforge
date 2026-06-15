/**
 * Client-side display sampling (frontend-architecture §7.5). Counters are always
 * EXACT; only the rendered ring buffer is sampled. Above 200 displayed events/s the
 * hook keeps every k-th event, `k = ceil(EPS / 200)`, deterministic by arrival index
 * (NOT random — stable under re-render).
 */

/** Sampling threshold in displayed events/second (§7.5). */
export const SAMPLE_THRESHOLD_EPS = 200;
/** Ring-buffer retention for the display list (§7.5). */
export const DEFAULT_BUFFER_SIZE = 1_000;
/** 4 Hz batched flush cadence into React state (§7.5). */
export const FLUSH_INTERVAL_MS = 250;
/** Sliding ingest-rate window (4 × 250 ms buckets = 1 s). */
export const EPS_WINDOW_MS = 1_000;

/** Sampling factor `k` for a measured ingest rate (1 = keep all). */
export function sampleFactor(eps: number): number {
  if (eps <= SAMPLE_THRESHOLD_EPS) return 1;
  return Math.ceil(eps / SAMPLE_THRESHOLD_EPS);
}

/** Whether the global arrival index survives the current factor (`index % k === 0`). */
export function keeps(arrivalIndex: number, factor: number): boolean {
  return factor <= 1 || arrivalIndex % factor === 0;
}

/**
 * A fixed-size ring buffer of the most recent events, newest last (§7.5). Pushing
 * past capacity drops the oldest from the DISPLAY only; counters are tracked
 * separately by the hook and are never affected.
 */
export class RingBuffer<T> {
  private items: T[] = [];

  constructor(private capacity: number) {}

  push(item: T): void {
    this.items.push(item);
    if (this.items.length > this.capacity) {
      this.items.splice(0, this.items.length - this.capacity);
    }
  }

  resize(capacity: number): void {
    this.capacity = capacity;
    if (this.items.length > capacity) {
      this.items.splice(0, this.items.length - capacity);
    }
  }

  clear(): void {
    this.items = [];
  }

  /** A snapshot array (newest last) — a fresh reference so React detects the change. */
  snapshot(): T[] {
    return this.items.slice();
  }

  get length(): number {
    return this.items.length;
  }
}

/**
 * Sliding events-per-second meter over 250 ms buckets (§7.5). `tick()` advances the
 * window to `now`; `add()` records arrivals into the current bucket.
 */
export class EpsMeter {
  private buckets: { t: number; n: number }[] = [];

  constructor(
    private windowMs = EPS_WINDOW_MS,
    private bucketMs = FLUSH_INTERVAL_MS,
  ) {}

  add(count: number, now: number): void {
    const slot = Math.floor(now / this.bucketMs);
    const last = this.buckets[this.buckets.length - 1];
    if (last && last.t === slot) last.n += count;
    else this.buckets.push({ t: slot, n: count });
    this.prune(now);
  }

  /** Current ingest rate (events/s) over the trailing window. */
  rate(now: number): number {
    this.prune(now);
    const total = this.buckets.reduce((sum, b) => sum + b.n, 0);
    return total / (this.windowMs / 1_000);
  }

  private prune(now: number): void {
    const oldest = now - this.windowMs;
    while (this.buckets.length > 0 && this.buckets[0].t * this.bucketMs < oldest) {
      this.buckets.shift();
    }
  }
}
