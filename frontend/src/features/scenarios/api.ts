/**
 * Scenarios feature data layer (frontend-architecture §9.4). Catalog + detail
 * are session-stable (5-min staleTime); manifests are immutable (Infinity,
 * INV-CAT-1). The registry browser is Phase 10 — not built here.
 */
import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { invalidate } from '../../shared/api/invalidation';
import { ApiError } from '../../shared/api/problem';
import { queryKeys, staleTimes } from '../../shared/api/queryKeys';
import type {
  Configuration,
  ConfigurationReplace,
  InstanceCreate,
  ManifestVersionDetail,
  ScenarioDetail,
  ScenarioInstance,
  ScenarioSummary,
} from '../../shared/api/types';

/** `['w', id, 'scenarios']` → catalog summaries (§9.4 CatalogGrid). */
export function scenariosQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.scenarios(wsId),
    staleTime: staleTimes.scenarios,
    queryFn: async (): Promise<ScenarioSummary[]> => {
      const { data, error } = await api.GET('/api/v1/scenarios');
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/** `['w', id, 'scenarios', slug]` → scenario detail with versions (§9.4 ScenarioDetail). */
export function scenarioQueryOptions(wsId: string, slug: string) {
  return queryOptions({
    queryKey: queryKeys.scenario(wsId, slug),
    staleTime: staleTimes.scenario,
    queryFn: async (): Promise<ScenarioDetail> => {
      const { data, error } = await api.GET('/api/v1/scenarios/{scenario_slug}', {
        params: { path: { scenario_slug: slug } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/** `['w', id, 'instances']` → instances in the workspace (§9.4 ScenarioDetail panel). */
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

/** `['w', id, 'instances', instanceId]` → a single instance resource (§9.4). */
export function instanceQueryOptions(wsId: string, instanceId: string) {
  return queryOptions({
    queryKey: queryKeys.instance(wsId, instanceId),
    queryFn: async (): Promise<ScenarioInstance> => {
      const { data, error } = await api.GET(
        '/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}',
        { params: { path: { workspace_id: wsId, scenario_instance_id: instanceId } } },
      );
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * `['w', id, 'instances', instanceId, 'config']` → the overlay document plus
 * `config_revision` (§9.4 InstanceConfigPage; api-spec §4.7 #36). Keyed off the
 * instance subtree so a successful save (which bumps config_revision) invalidates it.
 */
export function instanceConfigQueryOptions(wsId: string, instanceId: string) {
  return queryOptions({
    queryKey: [...queryKeys.instance(wsId, instanceId), 'config'] as const,
    queryFn: async (): Promise<Configuration> => {
      const { data, error } = await api.GET(
        '/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}/configuration',
        { params: { path: { workspace_id: wsId, scenario_instance_id: instanceId } } },
      );
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * `['w', id, 'scenarios', slug, 'manifest', version]` → the manifest version
 * document (immutable, staleTime Infinity per INV-CAT-1). The overlay editor reads
 * override bounds, catalog bounds, dwell families, CDC entities, and intensity
 * defaults out of this document (§9.4; overlay.ts readers).
 */
export function manifestQueryOptions(wsId: string, slug: string, version: string) {
  return queryOptions({
    queryKey: queryKeys.scenarioManifest(wsId, slug, version),
    staleTime: staleTimes.manifest,
    queryFn: async (): Promise<ManifestVersionDetail> => {
      const { data, error } = await api.GET(
        '/api/v1/scenarios/{scenario_slug}/versions/{manifest_version}',
        { params: { path: { scenario_slug: slug, manifest_version: version } } },
      );
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * Save the overlay (PUT configuration — FULL replacement, api-spec §4.7 #37). On
 * success the server returns the incremented `config_revision`; PIN-2 means running
 * streams keep their copied pin, so we invalidate ONLY the instance subtree (no
 * stream keys). A 422 manifest-validation-failed (MAN-V*, scope override) is handled
 * by the page's OverlayErrorMap.
 */
export function useSaveInstanceConfig(wsId: string, instanceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (configuration: ConfigurationReplace['configuration']): Promise<Configuration> => {
      const { data, error } = await api.PUT(
        '/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}/configuration',
        {
          params: { path: { workspace_id: wsId, scenario_instance_id: instanceId } },
          body: { configuration },
        },
      );
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: (config) => {
      qc.setQueryData([...queryKeys.instance(wsId, instanceId), 'config'], config);
      void invalidate.onInstanceConfigSaved(qc, wsId, instanceId);
    },
  });
}

/**
 * Create an instance with config defaults (§9.4 "create instance"). The deeper
 * overlay editor (ProbabilitySliders etc.) lives on the InstanceConfigPage; this
 * flow creates with the manifest defaults and routes there.
 */
export function useCreateInstance(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: InstanceCreate): Promise<ScenarioInstance> => {
      const { data, error } = await api.POST(
        '/api/v1/workspaces/{workspace_id}/scenario-instances',
        { params: { path: { workspace_id: wsId } }, body },
      );
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onInstancesChanged(qc, wsId),
  });
}
