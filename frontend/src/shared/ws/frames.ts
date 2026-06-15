/**
 * WebSocket live-tail protocol constants and frame types (frontend-architecture §7;
 * the frame and close-code catalog is owned by api-specification §5). These are the
 * client-side wire contract consumed by `TailSocket` (socket.ts) and `useStreamTail`.
 */

/** Versioned WS subprotocol — the server must select and echo it (WS-1, ADR-0013). */
export const TAIL_SUBPROTOCOL = 'dataforge.events.v1';

/** Frame discriminators of the tail protocol (frontend-architecture §7.3). */
export type TailFrameType =
  | 'ready'
  | 'resume_ack'
  | 'event'
  | 'drop_notice'
  | 'heartbeat'
  | 'error';

/** A delivered event envelope (all 20 fields, `_df` stripped per INV-DEL-2). */
export type DeliveredEnvelope = Record<string, unknown>;

/** The first frame the client sends (api-spec §5.2). Credentials never in the URL. */
export interface AuthFrame {
  type: 'auth';
  /** Console JWT (§6.5); never an API key from the browser. */
  access_token: string;
  /** Resume bookmark → `resume_ack` (§7.4). */
  cursor?: string;
  /** Server-side event-type filter (WS-5), ≤ 20 types. */
  types?: string[];
  /** Uniform server-side sampling (WS-2), 0 < r ≤ 1. */
  sample_rate?: number;
}

/** Server → client frames (api-spec §5.2). */
export interface ReadyFrame {
  type: 'ready';
  protocol: string;
  stream_id: string;
  position: { cursor: string };
  filters: { types?: string[]; sample_rate?: number };
}

export interface ResumeAckFrame {
  type: 'resume_ack';
  position: { cursor: string };
  behind: { events: number; from_cursor: string } | null;
}

export interface EventFrame {
  type: 'event';
  /** REST-compatible position AFTER this event — the resume bookmark. */
  cursor: string;
  event: DeliveredEnvelope;
}

export interface DropNoticeFrame {
  type: 'drop_notice';
  dropped: number;
  /** Position before the gap, for REST gap-fill (INV-DEL-5). */
  resume_cursor: string;
}

export interface HeartbeatFrame {
  type: 'heartbeat';
  server_time: string;
  last_cursor: string;
  delivered: number;
  dropped: number;
}

/** Carries an RFC 9457 problem document (e.g. `cursor-expired`, §7.4). */
export interface ErrorFrame {
  type: 'error';
  problem: {
    type?: string;
    title?: string;
    detail?: string;
    earliest_cursor?: string;
    retention_hours?: number;
    [key: string]: unknown;
  };
}

export type ServerFrame =
  | ReadyFrame
  | ResumeAckFrame
  | EventFrame
  | DropNoticeFrame
  | HeartbeatFrame
  | ErrorFrame;

/** Type guard: a parsed JSON value is a well-formed server frame. */
export function isServerFrame(value: unknown): value is ServerFrame {
  if (typeof value !== 'object' || value === null) return false;
  const t = (value as { type?: unknown }).type;
  return (
    t === 'ready' ||
    t === 'resume_ack' ||
    t === 'event' ||
    t === 'drop_notice' ||
    t === 'heartbeat' ||
    t === 'error'
  );
}
