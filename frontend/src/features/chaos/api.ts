/**
 * Chaos + answer-key data layer (frontend-architecture §9.5; chaos-engine §3.5, §7.3).
 * The ChaosPolicy is live-mutable (PIN-3) via `GET | PATCH /streams/{id}/chaos`; the
 * PATCH is optimistic with §4.3 invalidation. Answer-key reads are cursor-paginated
 * (ADR-0014 / queryKeys §4.2) with mode + time filters; the JSONL export streams the
 * same records for offline grading (chaos-engine §7.3).
 */
import {
  infiniteQueryOptions,
  queryOptions,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { invalidate } from '../../shared/api/invalidation';
import { ApiError } from '../../shared/api/problem';
import { queryKeys } from '../../shared/api/queryKeys';
import type {
  AnswerKeyInjection,
  AnswerKeyInjectionsPage,
  AnswerKeySummary,
  ChaosPolicyResponse,
} from '../../shared/api/types';
import type { ChaosPolicyDocument } from './types';

/** Filters shared by the answer-key summary, list, and export reads (chaos-engine §7.3). */
export interface AnswerKeyFilters {
  mode?: string;
  from?: string;
  to?: string;
  event_id?: string;
}

/** `['w', id, 'streams', sid, 'chaos']` → the live ChaosPolicy document (§4.8.3). */
export function chaosQueryOptions(wsId: string, streamId: string) {
  return queryOptions({
    queryKey: queryKeys.streamChaos(wsId, streamId),
    queryFn: async (): Promise<ChaosPolicyResponse> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}/chaos', {
        params: { path: { stream_id: streamId } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * Live ChaosPolicy PATCH (PIN-3, §4.8.3) — optimistic, then invalidates chaos + the
 * stream detail (§4.3 onChaosUpdated). The wire body is the loosely-typed document
 * (the contract types `modes` as free-form), so we cast the closed seven-mode shape.
 * 422 `validation-error` / `manifest-validation-failed` surface on the offending
 * control via the caller's onError (CH-V*).
 */
export function useUpdateChaos(wsId: string, streamId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (doc: ChaosPolicyDocument): Promise<ChaosPolicyResponse> => {
      const { data, error } = await api.PATCH('/api/v1/streams/{stream_id}/chaos', {
        params: { path: { stream_id: streamId } },
        // The contract omits a typed requestBody for chaos (free-form document).
        body: doc as never,
      });
      if (error) throw error as ApiError;
      return data;
    },
    onMutate: (doc) => {
      const prev = qc.getQueryData<ChaosPolicyResponse>(queryKeys.streamChaos(wsId, streamId));
      qc.setQueryData<ChaosPolicyResponse>(queryKeys.streamChaos(wsId, streamId), (cur) =>
        cur ? { ...cur, modes: doc } : cur,
      );
      return { prev };
    },
    onError: (_err, _doc, ctx) => {
      if (ctx?.prev) qc.setQueryData(queryKeys.streamChaos(wsId, streamId), ctx.prev);
    },
    onSuccess: (res) => {
      qc.setQueryData(queryKeys.streamChaos(wsId, streamId), res);
      void invalidate.onChaosUpdated(qc, wsId, streamId);
    },
  });
}

/** Per-mode injection counts (chaos-engine §7.3 summary). */
export function answerKeySummaryOptions(wsId: string, streamId: string, filters: AnswerKeyFilters) {
  return queryOptions({
    queryKey: queryKeys.streamAnswerKey(wsId, streamId, 'summary', filters),
    queryFn: async (): Promise<AnswerKeySummary> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}/answer-key/summary', {
        params: { path: { stream_id: streamId }, query: filters },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * Cursor-paginated injection list (ADR-0014). `useInfiniteQuery` per the §4.2 rule;
 * the cursor is opaque and never parsed client-side.
 */
export function answerKeyInjectionsOptions(
  wsId: string,
  streamId: string,
  filters: AnswerKeyFilters,
) {
  return infiniteQueryOptions({
    queryKey: queryKeys.streamAnswerKey(wsId, streamId, 'injections', filters),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }): Promise<AnswerKeyInjectionsPage> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}/answer-key/injections', {
        params: { path: { stream_id: streamId }, query: { ...filters, cursor: pageParam } },
      });
      if (error) throw error as ApiError;
      return data;
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Flatten the page list into the displayed rows. */
export function flattenInjections(pages: AnswerKeyInjectionsPage[]): AnswerKeyInjection[] {
  return pages.flatMap((p) => p.data);
}

/**
 * Download the filtered injections as JSONL (chaos-engine §7.3 export). Goes through
 * the typed client (IMP-4: only client.ts touches fetch) with `parseAs: 'blob'`, then
 * triggers a browser download. The same auth + audit applies as the paginated reads.
 */
export async function downloadAnswerKeyJsonl(
  streamId: string,
  filters: AnswerKeyFilters,
): Promise<void> {
  const { data, error } = await api.GET('/api/v1/streams/{stream_id}/answer-key/export', {
    params: { path: { stream_id: streamId }, query: filters },
    parseAs: 'blob',
  });
  if (error) throw error as ApiError;
  const url = URL.createObjectURL(data);
  const a = document.createElement('a');
  a.href = url;
  a.download = `answer-key-${streamId}.jsonl`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
