/**
 * WebSocket live-tail protocol constants (frontend-architecture §7; the frame and
 * close-code catalog is owned by api-specification §5).
 *
 * Phase 7 replaces this with the full frame payload types consumed by
 * `TailSocket` (socket.ts) and `useStreamTail`.
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
