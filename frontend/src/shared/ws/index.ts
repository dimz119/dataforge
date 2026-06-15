/**
 * WebSocket live-tail public surface (frontend-architecture §7). Features import
 * the hook + harness from here; the wire (`TailSocket`) and store internals are
 * available for tests and advanced composition.
 */
export { useStreamTail, type UseStreamTailOptions, type UseStreamTailResult } from './useStreamTail';
export { TailSocket, WS_CLOSE, type TailStatus, type WsTransport, type WsTransportFactory } from './socket';
export { TailStore, type TailNotice, type TailSnapshot, type TailCounters } from './tailStore';
export { FakeTailSocket, makeFakeFactory } from './fakeSocket';
export {
  TAIL_SUBPROTOCOL,
  isServerFrame,
  type ServerFrame,
  type DeliveredEnvelope,
  type AuthFrame,
  type ReadyFrame,
  type ResumeAckFrame,
  type EventFrame,
  type DropNoticeFrame,
  type HeartbeatFrame,
  type ErrorFrame,
} from './frames';
export {
  sampleFactor,
  keeps,
  SAMPLE_THRESHOLD_EPS,
  DEFAULT_BUFFER_SIZE,
  FLUSH_INTERVAL_MS,
} from './sampling';
export { gapFill, type GapFillResult } from './gapfill';
