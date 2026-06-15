/**
 * Monitoring data layer (frontend-architecture §9.7, §4). The overview table reads
 * the workspace stream list; the monitor header reads the single stream + its
 * authoritative stats (5 s poll, INV-OBS-2). Cross-feature imports are banned
 * (IMP-2), so this derives its reads from the shared query-key factory + the shared
 * `api` client — cache entries are shared by key with the dashboard/streams features.
 */
import { queryOptions } from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { POLL_STATS_MS, streamDetailInterval } from '../../shared/api/polling';
import { ApiError } from '../../shared/api/problem';
import { queryKeys, staleTimes } from '../../shared/api/queryKeys';
import type { StreamResponse, StreamStatsResponse } from '../../shared/api/types';

/** `['w', id, 'streams', null]` → all streams for the overview table. */
export function streamsQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.streams(wsId),
    refetchInterval: POLL_STATS_MS,
    queryFn: async (): Promise<StreamResponse[]> => {
      const { data, error } = await api.GET('/api/v1/streams', {
        params: { query: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/** `['w', id, 'streams', streamId]` → the single stream (monitor header), status-keyed poll. */
export function streamQueryOptions(wsId: string, streamId: string, status?: string) {
  return queryOptions({
    queryKey: queryKeys.stream(wsId, streamId),
    refetchInterval: streamDetailInterval(status),
    queryFn: async (): Promise<StreamResponse> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}', {
        params: { path: { stream_id: streamId } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/** `['w', id, 'streams', streamId, 'stats']` → authoritative counters, 5 s poll. */
export function streamStatsQueryOptions(wsId: string, streamId: string, enabled = true) {
  return queryOptions({
    queryKey: queryKeys.streamStats(wsId, streamId),
    staleTime: staleTimes.streamStats,
    refetchInterval: enabled ? POLL_STATS_MS : false,
    enabled,
    queryFn: async (): Promise<StreamStatsResponse> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}/stats', {
        params: { path: { stream_id: streamId } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}
