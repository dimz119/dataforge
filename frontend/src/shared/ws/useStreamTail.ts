/**
 * `useStreamTail` — the React binding for the live tail (frontend-architecture §7.6).
 *
 * Wires `TailSocket` (the wire) to `TailStore` (the state engine) and a 4 Hz flush
 * into React via `useSyncExternalStore`. Owns: the auth-token handoff to the socket,
 * REST gap-fill on `resume_ack.behind` / `drop_notice` (§7.4), and the Page Visibility
 * close-after-60 s-hidden rule. Page components consume the returned snapshot and
 * render only (§7.1).
 */
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';

import { tokenManager } from '../api/client';
import { gapFill } from './gapfill';
import type { DeliveredEnvelope, ServerFrame } from './frames';
import { FLUSH_INTERVAL_MS } from './sampling';
import { TailSocket, WS_CLOSE, type WsTransportFactory } from './socket';
import { TailStore, type TailNotice } from './tailStore';

export interface UseStreamTailOptions {
  /** Server-side filter (auth-frame `types`, WS-5) + defensive client filter. */
  eventTypes?: string[];
  /**
   * Per-entity CDC filter (auth-frame `entity_type`/`entity_key`, R-CDC-7). Both or
   * neither; matched against `entity_refs` with semantics identical to REST.
   */
  entityFilter?: { entityType: string; entityKey: string };
  /** Freeze the display buffer; counters keep counting (§7.6). */
  displayPaused?: boolean;
  /** Ring-buffer size (default 1000). */
  bufferSize?: number;
  /**
   * TEST SEAM ONLY: substitute the WebSocket transport with a FakeTailSocket factory
   * (§11.1). Production never sets this — the real `new WebSocket` (IMP-4) is used.
   */
  transportFactory?: WsTransportFactory;
}

export interface UseStreamTailResult {
  events: ReadonlyArray<DeliveredEnvelope>;
  status: 'connecting' | 'open' | 'reconnecting' | 'closed';
  counters: {
    received: number;
    displayed: number;
    sampledOut: number;
    droppedByServer: number;
    eps: number;
  };
  sampling: { active: boolean; keepRatio: number };
  lastCursor: string | null;
  notices: ReadonlyArray<TailNotice>;
  /** Terminal cross-tenant/not-found close (4403/4404) → render NotFound. */
  terminal: boolean;
  clear(): void;
}

const HIDDEN_CLOSE_MS = 60_000;

export function useStreamTail(
  streamId: string,
  opts: UseStreamTailOptions = {},
): UseStreamTailResult {
  const { eventTypes, entityFilter, displayPaused = false, bufferSize, transportFactory } = opts;
  const terminalRef = useRef(false);

  // A stable key so changing the filter recreates the socket+store (WS-5). The
  // per-entity CDC filter (R-CDC-7) is part of the filter set, so it joins the key.
  const typesKey = eventTypes?.length ? [...eventTypes].sort().join(',') : '';
  const entityKey = entityFilter ? `${entityFilter.entityType}:${entityFilter.entityKey}` : '';

  const store = useMemo(
    () => new TailStore({ bufferSize, eventTypes }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [streamId, typesKey, bufferSize],
  );

  useEffect(() => {
    store.setDisplayPaused(displayPaused);
  }, [store, displayPaused]);

  // 4 Hz batched flush into React state (§7.5).
  useEffect(() => {
    const id = setInterval(() => store.flush(), FLUSH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [store]);

  // The socket: one per (streamId, filter). Recreated when its deps change.
  useEffect(() => {
    terminalRef.current = false;
    let cancelled = false;
    const gapAborter = new AbortController();

    const socket = new TailSocket({
      streamId,
      types: eventTypes,
      entityType: entityFilter?.entityType,
      entityKey: entityFilter?.entityKey,
      transportFactory,
      getAccessToken: () => tokenManager.getValidAccessToken(),
      refreshToken: () => tokenManager.refresh(),
      handlers: {
        onStatus: (status) => store.setStatus(status),
        onFrame: (frame: ServerFrame) => {
          store.ingest(frame);
          if (frame.type === 'resume_ack' && frame.behind != null && frame.behind.events > 0) {
            void runGapFill(frame.behind.from_cursor);
          } else if (frame.type === 'drop_notice') {
            void runGapFill(frame.resume_cursor);
          }
        },
        onTerminal: () => {
          terminalRef.current = true;
          store.setStatus('closed');
        },
      },
    });

    async function runGapFill(fromCursor: string): Promise<void> {
      try {
        const result = await gapFill(
          streamId,
          fromCursor,
          socket.getCursor(),
          gapAborter.signal,
          {
            types: eventTypes,
            entityType: entityFilter?.entityType,
            entityKey: entityFilter?.entityKey,
          },
        );
        if (!cancelled) store.ingestGapFill(result.events);
      } catch {
        // cursor-expired / network: the inline notice already surfaced via the
        // error frame; REST recovery is best-effort (INV-DEL-5).
      }
    }

    socket.connect();

    // Page Visibility: close after 60 s hidden, reconnect (with cursor) on visible.
    let hideTimer: ReturnType<typeof setTimeout> | null = null;
    const onVisibility = () => {
      if (typeof document === 'undefined') return;
      if (document.hidden) {
        hideTimer = setTimeout(() => {
          socket.close();
          store.setStatus('reconnecting');
        }, HIDDEN_CLOSE_MS);
      } else {
        if (hideTimer != null) {
          clearTimeout(hideTimer);
          hideTimer = null;
        }
        socket.connect();
      }
    };
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }

    return () => {
      cancelled = true;
      gapAborter.abort();
      if (hideTimer != null) clearTimeout(hideTimer);
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
      socket.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, streamId, typesKey, entityKey]);

  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const clear = useCallback(() => store.clear(), [store]);

  return {
    events: snapshot.events,
    status: snapshot.status,
    counters: snapshot.counters,
    sampling: snapshot.sampling,
    lastCursor: snapshot.lastCursor,
    notices: snapshot.notices,
    terminal: terminalRef.current,
    clear,
  };
}

export { WS_CLOSE };
