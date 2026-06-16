/**
 * REST gap-fill for the live tail (frontend-architecture §7.4). The socket never
 * replays (ADR-0013): when `resume_ack.behind` is non-null, or a `drop_notice`
 * arrives, the missed window is recovered over REST — the WS cursor is
 * REST-interchangeable (api-spec §5.3). Pages are pulled from `from_cursor` via the
 * generated client (IMP-4: REST goes through `shared/api/client`, never raw fetch).
 */
import { api } from '../api/client';
import { ApiError } from '../api/problem';
import type { DeliveredEnvelope } from './frames';

/** A bound so a very large gap cannot stall the UI; the rest stays in REST. */
const MAX_GAP_PAGES = 50;
const PAGE_LIMIT = 1_000;

export interface GapFillResult {
  events: DeliveredEnvelope[];
  /** The cursor we reached (the resume bookmark after the last fetched event). */
  cursor: string;
  /** True when we stopped at the page bound before reaching `untilCursor`. */
  truncated: boolean;
}

/** The active tail filter set, resent on gap-fill so the bound cursor decodes (RC-7). */
export interface GapFillFilters {
  types?: string[];
  entityType?: string;
  entityKey?: string;
}

/**
 * Pull events over REST from `fromCursor` forward, stopping when we reach
 * `untilCursor` (the live tail position), exhaust the gap, or hit the page bound.
 * `cursor-expired` (410) is surfaced as the typed `ApiError` so the caller renders
 * the §7.4 teaching-moment notice and keeps tailing live.
 */
export async function gapFill(
  streamId: string,
  fromCursor: string,
  untilCursor: string | null,
  signal?: AbortSignal,
  filters?: GapFillFilters,
): Promise<GapFillResult> {
  const events: DeliveredEnvelope[] = [];
  let cursor = fromCursor;
  let pages = 0;

  while (pages < MAX_GAP_PAGES) {
    if (signal?.aborted) break;
    const { data, error } = await api.GET('/api/v1/streams/{stream_id}/events', {
      params: {
        path: { stream_id: streamId },
        // The cursor's fingerprint binds to the filter set (RC-7); the gap-fill MUST
        // resend the SAME types + per-entity CDC filter or the cursor fails to decode
        // (400 cursor-invalid). This keeps REST gap-fill identical to the WS slice.
        query: {
          cursor,
          limit: PAGE_LIMIT,
          ...(filters?.types?.length ? { types: filters.types.join(',') } : {}),
          ...(filters?.entityType && filters?.entityKey
            ? { entity_type: filters.entityType, entity_key: filters.entityKey }
            : {}),
        },
      },
      signal,
    });
    if (error) throw error as ApiError;

    for (const row of data.data) events.push(row);
    pages += 1;
    const next = data.next_cursor;

    // An empty poll returns the SAME cursor (E-1) → we have caught up to live.
    if (next === cursor || data.data.length === 0) {
      cursor = next;
      return { events, cursor, truncated: false };
    }
    cursor = next;
    // Reached (or passed) the socket's live position → hand back to the socket.
    if (untilCursor != null && cursor === untilCursor) {
      return { events, cursor, truncated: false };
    }
  }
  return { events, cursor, truncated: true };
}
