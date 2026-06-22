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
import type {
  Quota,
  QuotaLimit,
  StreamResponse,
  StreamStatsResponse,
  Workspace,
  WorkspaceQuotaUsage,
} from '../../shared/api/types';

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

/** Narrow one entry of the opaque `Quota.quotas` dict to a `{limit, used?}` line. */
function quotaLine(raw: unknown): QuotaLimit {
  if (raw && typeof raw === 'object') {
    const obj = raw as Record<string, unknown>;
    const limit = typeof obj.limit === 'number' ? obj.limit : 0;
    const used = typeof obj.used === 'number' ? obj.used : undefined;
    return { limit, used };
  }
  return { limit: 0 };
}

/** Project the opaque `Quota.quotas` dict onto the three meter-relevant quotas. */
export function quotaUsage(quota: Quota): WorkspaceQuotaUsage {
  const q = quota.quotas as Record<string, unknown>;
  return {
    events_per_day: quotaLine(q.events_per_day),
    aggregate_tps_cap: quotaLine(q.aggregate_tps_cap),
    concurrent_streams: quotaLine(q.concurrent_streams),
  };
}

/**
 * `['w', id, 'quotas']` → the workspace's quota limits + live usage (P11). NOT a list
 * endpoint — the response is the `Quota` resource directly, so the queryFn returns
 * `data` (no `.data` envelope unwrap). `used` for events/day comes from the Redis day
 * meter; aggregate-TPS + concurrent-streams from the live occupied-stream rows. Polled
 * on the same 5 s cadence as stats so the dashboard meters stay fresh while mounted.
 */
export function quotasQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.quotas(wsId),
    staleTime: staleTimes.streamStats,
    refetchInterval: POLL_STATS_MS,
    queryFn: async (): Promise<Quota> => {
      const { data, error } = await api.GET('/api/v1/workspaces/{workspace_id}/quotas', {
        params: { path: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data;
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
