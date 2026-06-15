/**
 * Dashboard data layer (frontend-architecture §9.2, §4). Reads the workspace stream
 * list and per-stream stats (5 s poll, INV-OBS-2). Cross-feature imports are banned
 * (IMP-2), so the monitoring overview and this dashboard each derive their reads from
 * the shared query-key factory + the shared `api` client — the CACHE entries are
 * shared by key, not by import.
 */
import { queryOptions } from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { POLL_STATS_MS } from '../../shared/api/polling';
import { ApiError } from '../../shared/api/problem';
import { queryKeys, staleTimes } from '../../shared/api/queryKeys';
import type { StreamResponse, StreamStatsResponse, Workspace } from '../../shared/api/types';

/**
 * `['w', id, 'detail']` → the workspace resource (member_count, plan) for the summary
 * card. The streams list is workspace-scoped server-side (the JWT carries the active
 * workspace), so the endpoint takes no query param.
 */
export function workspaceDetailQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.workspaceDetail(wsId),
    queryFn: async (): Promise<Workspace> => {
      const { data, error } = await api.GET('/api/v1/workspaces/{workspace_id}', {
        params: { path: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * `['w', id, 'streams', null]` → the workspace's streams (dashboard cards + overview).
 * The flat collection route is workspace-scoped by the required `workspace_id` query
 * param (W-2); the response is the `{data, next_cursor}` page envelope (api-spec §2.6).
 */
export function streamsQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.streams(wsId),
    queryFn: async (): Promise<StreamResponse[]> => {
      const { data, error } = await api.GET('/api/v1/streams', {
        params: { query: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/**
 * `['w', id, 'streams', streamId, 'stats']` → the authoritative Redis counters
 * (total_events, observed_tps, by_event_type, last_event_at, health). staleTime 0 +
 * 5 s poll (§4.4): always fresh while a card is mounted. `enabled` lets cards skip
 * the poll for settled streams that will never produce stats.
 */
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
