/**
 * `FakeTailSocket` — a test/dev harness implementing the `WsTransport` surface
 * (frontend-architecture §7.1, §11.1). It lets tests drive `TailSocket` and
 * `useStreamTail` deterministically with fake timers — no real WebSocket (IMP-4:
 * the real `new WebSocket` lives only in socket.ts). Tests inject `factory()` as the
 * `transportFactory` and then push server frames / closes through the returned handle.
 */
import { TAIL_SUBPROTOCOL, type ServerFrame } from './frames';
import type { WsTransport, WsTransportFactory } from './socket';

/** A captured auth (or resume) frame the client sent on the fake socket. */
export interface CapturedSend {
  raw: string;
  parsed: Record<string, unknown>;
}

export class FakeTailSocket implements WsTransport {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;

  /** Every frame the client sent (the first is the `auth` frame, §7.2). */
  readonly sent: CapturedSend[] = [];
  closed: { code?: number; reason?: string } | null = null;

  constructor(
    readonly url: string,
    /** The subprotocol the server "selects"; '' simulates no selection (§7.2). */
    readonly protocol: string = TAIL_SUBPROTOCOL,
  ) {}

  send(data: string): void {
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(data) as Record<string, unknown>;
    } catch {
      // leave parsed empty
    }
    this.sent.push({ raw: data, parsed });
  }

  close(code?: number, reason?: string): void {
    this.closed = { code, reason };
  }

  // --- Test drivers (server → client) -------------------------------------

  /** Fire `onopen` — the client then sends its auth frame from the handler. */
  open(): void {
    this.onopen?.();
  }

  /** Deliver one server frame to the client. */
  emit(frame: ServerFrame): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  /** Deliver a raw text frame (for malformed-frame tests). */
  emitRaw(data: string): void {
    this.onmessage?.({ data });
  }

  /** Simulate a server close with a code (§7.4 reconnect/reauth/terminal paths). */
  serverClose(code: number, reason = ''): void {
    this.onclose?.({ code, reason });
  }

  /** The first captured send, parsed as the auth frame. */
  authFrame(): Record<string, unknown> | null {
    return this.sent[0]?.parsed ?? null;
  }
}

/**
 * A factory that records every fake socket it builds, so a test can drive the
 * latest connection across reconnects. Pass `factory` as `transportFactory`.
 */
export function makeFakeFactory(opts?: { protocol?: string }): {
  factory: WsTransportFactory;
  sockets: FakeTailSocket[];
  last(): FakeTailSocket;
} {
  const sockets: FakeTailSocket[] = [];
  const factory: WsTransportFactory = (url) => {
    const sock = new FakeTailSocket(url, opts?.protocol ?? TAIL_SUBPROTOCOL);
    sockets.push(sock);
    return sock;
  };
  return {
    factory,
    sockets,
    last: () => sockets[sockets.length - 1],
  };
}
