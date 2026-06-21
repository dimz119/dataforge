/**
 * Streams feature data layer (frontend-architecture §9.5, §4). Stream detail polls
 * on a status-keyed interval (§4.4 streamDetailInterval): 2 s while converging,
 * 10 s while running, off when settled. Lifecycle verbs are idempotent desired-state
 * POSTs (INV-STR-3); the PATCH is the live target_tps mutation (PIN-3, §4.8.2).
 */
import {
  queryOptions,
  useMutation,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { invalidate } from '../../shared/api/invalidation';
import { ApiError } from '../../shared/api/problem';
import { queryKeys } from '../../shared/api/queryKeys';
import { streamDetailInterval } from '../../shared/api/polling';
import type {
  ScenarioInstance,
  SchemaUpgradeCreate,
  SchemaUpgradeResponse,
  StreamCreate,
  StreamResponse,
  StreamSchemaVersionsResponse,
  SubjectSummary,
} from '../../shared/api/types';

/**
 * `['w', id, 'instances']` → the workspace's scenario instances, for the
 * CreateStreamPage instance picker (§9.5). The InstanceConfigPage (features/scenarios)
 * owns the same query under the same key; feature boundaries (IMP-2) forbid importing
 * across features, so each feature derives its read from the shared query-key factory —
 * the cache entry is shared by key, not by import.
 */
export function instancesQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.instances(wsId),
    queryFn: async (): Promise<ScenarioInstance[]> => {
      const { data, error } = await api.GET(
        '/api/v1/workspaces/{workspace_id}/scenario-instances',
        { params: { path: { workspace_id: wsId } } },
      );
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

// The stream-LIST query + the monitoring overview live with the next agent
// (features/monitoring, §9.7). This module owns only the control-panel surface
// (§9.5): single-stream detail, create, lifecycle verbs, and live target_tps.

/**
 * `['w', id, 'streams', streamId]` → the single stream resource. The poll interval
 * is status-driven (§4.4): the control panel passes the current status so the
 * StatusBadge converges within 2 s during a transition then settles.
 */
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

/** Create a stream (§9.5 CreateStreamPage). Copies the instance pin at creation (T1). */
export function useCreateStream(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: StreamCreate): Promise<StreamResponse> => {
      const { data, error } = await api.POST('/api/v1/streams', { body });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onStreamCreated(qc, wsId),
  });
}

/** The lifecycle verbs share one POST shape (api-spec §4.8.1). */
type LifecycleVerb = 'start' | 'pause' | 'resume' | 'stop';

const VERB_PATH = {
  start: '/api/v1/streams/{stream_id}/start',
  pause: '/api/v1/streams/{stream_id}/pause',
  resume: '/api/v1/streams/{stream_id}/resume',
  stop: '/api/v1/streams/{stream_id}/stop',
} as const;

/** Optimistically reflect the desired run-state so the matrix flips to `pending`. */
function setStreamCache(
  qc: QueryClient,
  wsId: string,
  streamId: string,
  status: string,
): void {
  qc.setQueryData<StreamResponse>(queryKeys.stream(wsId, streamId), (prev) =>
    prev ? { ...prev, status } : prev,
  );
}

/**
 * Issue a lifecycle verb (§9.5 LifecycleButtons). Each verb is idempotent
 * (INV-STR-3); the 200 returns the resource with the new desired state and the
 * convergence poll (§4.4) tracks the status to its terminal value. We optimistically
 * set the in-flight status (`starting`/`pausing`/`resuming`/`stopping`) so the
 * button enters its `pending` matrix cell immediately.
 */
export function useStreamLifecycle(wsId: string, streamId: string) {
  const qc = useQueryClient();
  const transitionStatus: Record<LifecycleVerb, string> = {
    start: 'starting',
    pause: 'pausing',
    resume: 'resuming',
    stop: 'stopping',
  };
  return useMutation({
    mutationFn: async (verb: LifecycleVerb): Promise<StreamResponse> => {
      setStreamCache(qc, wsId, streamId, transitionStatus[verb]);
      const { data, error } = await api.POST(VERB_PATH[verb], {
        params: { path: { stream_id: streamId } },
      });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: (stream) => {
      qc.setQueryData(queryKeys.stream(wsId, streamId), stream);
      void invalidate.onStreamLifecycle(qc, wsId, streamId);
    },
    onError: () => invalidate.onStreamLifecycle(qc, wsId, streamId),
  });
}

/**
 * Live target_tps mutation (PATCH, §4.8.2, PIN-3) — the TpsSlider's debounced,
 * optimistic write. Out of range 1..1,000 → 400 validation-error; above the plan
 * cap → 403 quota-exceeded (surfaced as a toast by the slider). The runner applies
 * the new rate within 2 s.
 */
export function useSetTargetTps(wsId: string, streamId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (targetTps: number): Promise<StreamResponse> => {
      const { data, error } = await api.PATCH('/api/v1/streams/{stream_id}', {
        params: { path: { stream_id: streamId } },
        body: { target_tps: targetTps },
      });
      if (error) throw error as ApiError;
      return data;
    },
    onMutate: (targetTps) => {
      const prev = qc.getQueryData<StreamResponse>(queryKeys.stream(wsId, streamId));
      if (prev) {
        qc.setQueryData<StreamResponse>(queryKeys.stream(wsId, streamId), {
          ...prev,
          desired_state: { ...prev.desired_state, target_tps: targetTps },
        });
      }
      return { prev };
    },
    onError: (_err, _tps, ctx) => {
      if (ctx?.prev) qc.setQueryData(queryKeys.stream(wsId, streamId), ctx.prev);
    },
    onSuccess: (stream) => qc.setQueryData(queryKeys.stream(wsId, streamId), stream),
  });
}

// Stream DELETE (T14, §9.5 StreamDangerZone delete) is NOT in the MVP OpenAPI
// contract — `DELETE /streams/{stream_id}` is absent from the generated client and
// the backend StreamDetailView exposes only GET | PATCH. The danger-zone delete
// control therefore renders disabled with an explanatory note until the endpoint is
// added to the contract; `invalidate.onStreamDeleted` is wired and waiting. Adding a
// real delete is a backend/contract change, out of scope for this frontend phase.

// ───────────────────────────────────────────────────────────────────────────
// Schema pinning + scheduled upgrades (Phase 10, §10). The schema-versions
// projection (effective/pending/applied) follows the same status-keyed poll as the
// stream resource so the pending → applied cutover surfaces live; scheduling validates
// REG-U001..U007 server-side (409 conflict + errors[]).
// ───────────────────────────────────────────────────────────────────────────

/**
 * `['w', id, 'streams', sid, 'schema-versions']` → the §10.2 effective-version map plus
 * the pending/applied upgrade entries. Polled on the same status-keyed interval as the
 * stream resource (§4.4) so the simulated-time cutover (pending → applied) is observed
 * without a manual refresh.
 */
export function streamSchemaVersionsQueryOptions(wsId: string, streamId: string, status?: string) {
  return queryOptions({
    queryKey: queryKeys.streamSchemaVersions(wsId, streamId),
    refetchInterval: streamDetailInterval(status),
    queryFn: async (): Promise<StreamSchemaVersionsResponse> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}/schema-versions', {
        params: { path: { stream_id: streamId } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * `['w', id, 'streams', sid, 'schema-upgrades']` → the full upgrade history (scheduled,
 * applied, AND cancelled — cancelled entries are retained, §10.3). The schema panel
 * folds these with the effective map to render the timeline + the picker eligibility.
 */
export function streamSchemaUpgradesQueryOptions(wsId: string, streamId: string) {
  return queryOptions({
    queryKey: queryKeys.streamSchemaUpgrades(wsId, streamId),
    queryFn: async (): Promise<SchemaUpgradeResponse[]> => {
      const { data, error } = await api.GET('/api/v1/streams/{stream_id}/schema-upgrades', {
        params: { path: { stream_id: streamId } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/**
 * `['w', id, 'schemas']` → the registry subject list, reused by the scheduling form's
 * subject picker. Feature boundaries (IMP-2) forbid importing the scenarios feature's
 * `subjectsQueryOptions`, so we re-derive the same read under the same shared key — the
 * cache entry is shared by key, not by import.
 */
export function subjectsQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.schemas(wsId),
    queryFn: async (): Promise<SubjectSummary[]> => {
      const { data, error } = await api.GET('/api/v1/schemas');
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/**
 * Schedule a mid-stream schema upgrade (POST, api-spec §4.8.4 #50). `at` is the
 * SIMULATED-time cutover instant (RFC 3339, occurred_at domain) — omit for "next tick".
 * REG-U001..U007 violations surface as a 409 `conflict` with `errors[]` (rendered inline
 * by the form). On success we invalidate the upgrades list, the schema-versions
 * projection, and the stream resource.
 *
 * NOTE: the backend accepts an optional `Idempotency-Key` header (I-1), but the OpenAPI
 * contract does not document it as a parameter (`header?: never` on the generated op), so
 * the typed client cannot forward it. Idempotent replay is therefore not exercised from
 * the console; a contract change would be required to surface it. Recorded for verify.
 */
export function useScheduleSchemaUpgrade(wsId: string, streamId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: SchemaUpgradeCreate): Promise<SchemaUpgradeResponse> => {
      const { data, error } = await api.POST('/api/v1/streams/{stream_id}/schema-upgrades', {
        params: { path: { stream_id: streamId } },
        body,
      });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onSchemaUpgradeChanged(qc, wsId, streamId),
  });
}

/**
 * Cancel a scheduled upgrade (DELETE, #52) → 204. Only a `scheduled` entry is
 * cancellable; otherwise the server returns 409 `invalid-state-transition`. Cancelled
 * entries are retained in the list (§10.3), so we invalidate rather than remove.
 */
export function useCancelSchemaUpgrade(wsId: string, streamId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (upgradeId: string): Promise<void> => {
      const { error } = await api.DELETE(
        '/api/v1/streams/{stream_id}/schema-upgrades/{upgrade_id}',
        { params: { path: { stream_id: streamId, upgrade_id: upgradeId } } },
      );
      if (error) throw error as ApiError;
    },
    onSuccess: () => invalidate.onSchemaUpgradeChanged(qc, wsId, streamId),
  });
}
