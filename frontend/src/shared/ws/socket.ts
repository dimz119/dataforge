/**
 * `TailSocket` — one live-tail WebSocket connection (frontend-architecture §7.1/§7.4).
 *
 * Framework-free, unit-testable with fake timers. Responsibilities: subprotocol
 * verification, first-message auth (§7.2), frame parsing (§7.3), heartbeat watchdog,
 * reconnect with full-jitter backoff (§7.4), cursor tracking + REST gap-fill handoff.
 * IMP-4: this is the ONLY module permitted to construct a `WebSocket`; the transport
 * is injectable so tests (and the FakeTailSocket harness) substitute a fake without
 * touching a real socket.
 */
import {
  TAIL_SUBPROTOCOL,
  isServerFrame,
  type AuthFrame,
  type ServerFrame,
} from './frames';

/** WS close codes the client reacts to (api-spec §5.5). */
export const WS_CLOSE = {
  GOING_AWAY: 1001,
  REAUTH: 4401,
  FORBIDDEN: 4403,
  NOT_FOUND: 4404,
  AUTH_DEADLINE: 4408,
  CONN_LIMIT: 4429,
} as const;

/** Connection lifecycle reported to the React binding (§7.6 `status`). */
export type TailStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

/** Minimal WebSocket surface TailSocket depends on (so a fake can substitute). */
export interface WsTransport {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: ((ev: { code: number; reason: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  /** The subprotocol the server selected (echoed); '' when none (§7.2). */
  readonly protocol: string;
}

/** Builds a transport for a URL + subprotocols. Default wraps the real WebSocket. */
export type WsTransportFactory = (url: string, protocols: string[]) => WsTransport;

/** Default factory — the one sanctioned `new WebSocket(...)` in the codebase (IMP-4). */
export const defaultTransportFactory: WsTransportFactory = (url, protocols) => {
  const ws = new WebSocket(url, protocols);
  const transport: WsTransport = {
    send: (data) => ws.send(data),
    close: (code, reason) => ws.close(code, reason),
    get protocol() {
      return ws.protocol;
    },
    set onopen(fn: (() => void) | null) {
      ws.onopen = fn;
    },
    get onopen() {
      return ws.onopen as (() => void) | null;
    },
    set onmessage(fn: ((ev: { data: string }) => void) | null) {
      ws.onmessage = fn as ((ev: MessageEvent) => void) | null;
    },
    get onmessage() {
      return ws.onmessage as ((ev: { data: string }) => void) | null;
    },
    set onclose(fn: ((ev: { code: number; reason: string }) => void) | null) {
      ws.onclose = fn as ((ev: CloseEvent) => void) | null;
    },
    get onclose() {
      return ws.onclose as ((ev: { code: number; reason: string }) => void) | null;
    },
    set onerror(fn: ((ev: unknown) => void) | null) {
      ws.onerror = fn as ((ev: Event) => void) | null;
    },
    get onerror() {
      return ws.onerror as ((ev: unknown) => void) | null;
    },
  };
  return transport;
};

/** Callbacks the React binding registers (§7.1: page components render only). */
export interface TailSocketHandlers {
  onStatus(status: TailStatus): void;
  onFrame(frame: ServerFrame): void;
  /** Terminal close (4403/4404): render NotFound presentation, no reconnect. */
  onTerminal(code: number): void;
}

export interface TailSocketOptions {
  streamId: string;
  /** Resolves a fresh, valid console JWT — TokenManager.getValidAccessToken (§6.3). */
  getAccessToken(): Promise<string>;
  /** Forces a token refresh after a 4401 close (§7.4 reauth). */
  refreshToken(): Promise<string>;
  /** Server-side event-type filter (auth-frame `types`, WS-5). */
  types?: string[];
  /** Server-side uniform sampling (auth-frame `sample_rate`, WS-2). */
  sampleRate?: number;
  handlers: TailSocketHandlers;
  /** Injected for tests; defaults to the real WebSocket (IMP-4). */
  transportFactory?: WsTransportFactory;
  /** Base WS URL; defaults to the same-origin `/ws` proxy (vite.config.ts). */
  wsBase?: string;
  /** Injected timers for fake-timer tests. */
  setTimeoutFn?: (cb: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeoutFn?: (h: ReturnType<typeof setTimeout>) => void;
  /** Injected RNG for deterministic jitter in tests (default `Math.random`). */
  random?: () => number;
}

/** Timing constants (§7.3/§7.4). */
const AUTH_DEADLINE_MS = 10_000;
const WATCHDOG_MS = 45_000; // 3 missed 15 s heartbeats
const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const STABLE_RESET_MS = 60_000; // attempt counter resets after a stable connection

/** Same-origin `/ws` by default (Vite proxies it to the WS gateway). */
function defaultWsBase(): string {
  if (typeof location !== 'undefined') {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}`;
  }
  return 'ws://localhost';
}

/**
 * One reconnecting tail connection. Created by `useStreamTail` (one per subscribe);
 * a `types`/`sampleRate` change recreates the socket (WS-5). All timing is injectable
 * so the unit tests drive it with vitest fake timers and the FakeTailSocket harness.
 */
export class TailSocket {
  private readonly opts: TailSocketOptions;
  private readonly factory: WsTransportFactory;
  private readonly wsBase: string;
  private readonly setTimeoutFn: (cb: () => void, ms: number) => ReturnType<typeof setTimeout>;
  private readonly clearTimeoutFn: (h: ReturnType<typeof setTimeout>) => void;
  private readonly random: () => number;

  private transport: WsTransport | null = null;
  private status: TailStatus = 'closed';
  private attempt = 0;
  private lastCursor: string | null = null;
  private closedByUser = false;

  private authTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private backoffTimer: ReturnType<typeof setTimeout> | null = null;
  private stableTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(opts: TailSocketOptions) {
    this.opts = opts;
    this.factory = opts.transportFactory ?? defaultTransportFactory;
    this.wsBase = opts.wsBase ?? defaultWsBase();
    this.setTimeoutFn = opts.setTimeoutFn ?? ((cb, ms) => setTimeout(cb, ms));
    this.clearTimeoutFn = opts.clearTimeoutFn ?? ((h) => clearTimeout(h));
    this.random = opts.random ?? (() => Math.random());
  }

  /** Resume bookmark — REST-interchangeable cursor of the last delivered event. */
  getCursor(): string | null {
    return this.lastCursor;
  }

  /** Seed a resume cursor (e.g. from a prior subscribe) before connecting. */
  setCursor(cursor: string | null): void {
    this.lastCursor = cursor;
  }

  /** Open the connection (idempotent — a no-op while one is live). */
  connect(): void {
    if (this.transport != null) return;
    this.closedByUser = false;
    void this.openConnection();
  }

  /** User/unmount close — terminal, no reconnect (§7.4 `closed`). */
  close(): void {
    this.closedByUser = true;
    this.clearAllTimers();
    this.teardownTransport(1000, 'client closed');
    this.setStatus('closed');
  }

  private setStatus(status: TailStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.opts.handlers.onStatus(status);
  }

  private async openConnection(): Promise<void> {
    this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');
    let token: string;
    try {
      token = await this.opts.getAccessToken();
    } catch {
      this.scheduleReconnect();
      return;
    }
    if (this.closedByUser) return;

    const url = `${this.wsBase}/ws/streams/${this.opts.streamId}/events`;
    const transport = this.factory(url, [TAIL_SUBPROTOCOL]);
    this.transport = transport;

    transport.onopen = () => this.handleOpen(token);
    transport.onmessage = (ev) => this.handleMessage(ev.data);
    transport.onclose = (ev) => this.handleClose(ev.code);
    transport.onerror = () => {
      // An error precedes a close; the close handler drives reconnect.
    };

    // Auth deadline: the server closes 4408 after 10 s; we self-guard too.
    this.authTimer = this.setTimeoutFn(() => {
      this.teardownTransport(WS_CLOSE.AUTH_DEADLINE, 'auth deadline');
      this.scheduleReconnect();
    }, AUTH_DEADLINE_MS);
  }

  private handleOpen(token: string): void {
    const transport = this.transport;
    if (!transport) return;
    // The server must select+echo the versioned subprotocol (§7.2); otherwise abort.
    if (transport.protocol !== '' && transport.protocol !== TAIL_SUBPROTOCOL) {
      this.teardownTransport(WS_CLOSE.AUTH_DEADLINE, 'bad subprotocol');
      this.scheduleReconnect();
      return;
    }
    const frame: AuthFrame = {
      type: 'auth',
      access_token: token,
      cursor: this.lastCursor ?? undefined,
      types: this.opts.types?.length ? this.opts.types : undefined,
      sample_rate: this.opts.sampleRate,
    };
    transport.send(JSON.stringify(frame));
    // `open` is reported only after the server's `ready` frame (§7.2).
    this.armWatchdog();
  }

  private handleMessage(data: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return; // malformed text frame — ignore (server enforces protocol)
    }
    if (!isServerFrame(parsed)) return;
    const frame = parsed;
    this.armWatchdog(); // any frame resets liveness (§7.3 watchdog)

    switch (frame.type) {
      case 'ready':
        if (this.authTimer != null) {
          this.clearTimeoutFn(this.authTimer);
          this.authTimer = null;
        }
        if (this.lastCursor == null) this.lastCursor = frame.position.cursor;
        this.markOpen();
        break;
      case 'event':
        this.lastCursor = frame.cursor;
        break;
      case 'drop_notice':
      case 'resume_ack':
      case 'heartbeat':
      case 'error':
        break;
    }
    this.opts.handlers.onFrame(frame);
  }

  private markOpen(): void {
    this.setStatus('open');
    // Reset the attempt counter once the connection stays open ≥ 60 s (§7.4).
    if (this.stableTimer != null) this.clearTimeoutFn(this.stableTimer);
    this.stableTimer = this.setTimeoutFn(() => {
      this.attempt = 0;
    }, STABLE_RESET_MS);
  }

  private handleClose(code: number): void {
    this.clearTransportTimers();
    this.transport = null;
    if (this.closedByUser) return;

    if (code === WS_CLOSE.FORBIDDEN || code === WS_CLOSE.NOT_FOUND) {
      // Terminal: cross-tenant masking / missing → NotFound presentation (§7.4).
      this.clearAllTimers();
      this.setStatus('closed');
      this.opts.handlers.onTerminal(code);
      return;
    }
    if (code === WS_CLOSE.REAUTH) {
      void this.reauthThenReconnect();
      return;
    }
    if (code === WS_CLOSE.GOING_AWAY) {
      // Deploy/going-away → immediate reconnect, no backoff (§7.4).
      this.setStatus('reconnecting');
      void this.openConnection();
      return;
    }
    // 1006/1011/1013/4408/4429/watchdog → jittered backoff.
    this.scheduleReconnect();
  }

  private async reauthThenReconnect(): Promise<void> {
    this.setStatus('reconnecting');
    try {
      await this.opts.refreshToken();
    } catch {
      // Refresh failed → fall through to backoff; bootstrap/middleware handle logout.
    }
    if (this.closedByUser) return;
    void this.openConnection();
  }

  private scheduleReconnect(): void {
    this.teardownTransport(1000, 'reconnect');
    if (this.closedByUser) return;
    this.setStatus('reconnecting');
    const delay = Math.min(BACKOFF_BASE_MS * 2 ** this.attempt, BACKOFF_MAX_MS);
    const jittered = this.random() * delay; // full jitter (§7.4)
    this.attempt += 1;
    if (this.backoffTimer != null) this.clearTimeoutFn(this.backoffTimer);
    this.backoffTimer = this.setTimeoutFn(() => {
      this.backoffTimer = null;
      if (!this.closedByUser) void this.openConnection();
    }, jittered);
  }

  private armWatchdog(): void {
    if (this.watchdogTimer != null) this.clearTimeoutFn(this.watchdogTimer);
    this.watchdogTimer = this.setTimeoutFn(() => {
      // No frame for 45 s → presume dead; close and reconnect (§7.3).
      this.teardownTransport(1000, 'watchdog');
      this.scheduleReconnect();
    }, WATCHDOG_MS);
  }

  private clearTransportTimers(): void {
    if (this.authTimer != null) {
      this.clearTimeoutFn(this.authTimer);
      this.authTimer = null;
    }
    if (this.watchdogTimer != null) {
      this.clearTimeoutFn(this.watchdogTimer);
      this.watchdogTimer = null;
    }
    if (this.stableTimer != null) {
      this.clearTimeoutFn(this.stableTimer);
      this.stableTimer = null;
    }
  }

  private clearAllTimers(): void {
    this.clearTransportTimers();
    if (this.backoffTimer != null) {
      this.clearTimeoutFn(this.backoffTimer);
      this.backoffTimer = null;
    }
  }

  private teardownTransport(code: number, reason: string): void {
    const transport = this.transport;
    this.clearTransportTimers();
    if (transport) {
      transport.onopen = null;
      transport.onmessage = null;
      transport.onclose = null;
      transport.onerror = null;
      try {
        transport.close(code, reason);
      } catch {
        // already closing
      }
      this.transport = null;
    }
  }
}
