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
import { skipToken } from '@tanstack/react-query';
import type {
  Configuration,
  ConfigurationReplace,
  InstanceCreate,
  ManifestVersionDetail,
  ScenarioDetail,
  ScenarioInstance,
  ScenarioSummary,
  SchemaDiff,
  SubjectDetail,
  SubjectSummary,
  VersionProvenance,
  VersionRecord,
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

// ───────────────────────────────────────────────────────────────────────────
// Schema registry (Phase 10, frontend-architecture §3.1/§9.4). Subjects + version
// timelines are session-stable (5-min staleTime); a single version document and the
// version-to-version diff are IMMUTABLE (staleTime Infinity, INV-REG-2/3) — once a
// version is registered it never changes, and the additive diff is a pure function of
// two immutable documents. All four reads need `schemas:read`.
// ───────────────────────────────────────────────────────────────────────────

/** `['w', id, 'schemas']` → the registry subject list (#62, RegistryBrowserPage table). */
export function subjectsQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.schemas(wsId),
    staleTime: staleTimes.schemas,
    queryFn: async (): Promise<SubjectSummary[]> => {
      const { data, error } = await api.GET('/api/v1/schemas');
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/** `['w', id, 'schemas', subject]` → subject detail with per-version provenance (#63). */
export function subjectQueryOptions(wsId: string, subject: string) {
  return queryOptions({
    queryKey: queryKeys.schema(wsId, subject),
    staleTime: staleTimes.schemas,
    queryFn: async (): Promise<SubjectDetail> => {
      const { data, error } = await api.GET('/api/v1/schemas/{subject}', {
        params: { path: { subject } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/** `['w', id, 'schemas', subject, 'versions']` → the version provenance rows (#64). */
export function subjectVersionsQueryOptions(wsId: string, subject: string) {
  return queryOptions({
    queryKey: queryKeys.schemaVersions(wsId, subject),
    staleTime: staleTimes.schemaVersions,
    queryFn: async (): Promise<VersionProvenance[]> => {
      const { data, error } = await api.GET('/api/v1/schemas/{subject}/versions', {
        params: { path: { subject } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/**
 * `['w', id, 'schemas', subject, 'versions', version]` → one version's full record
 * including the schema document (#65). Immutable (staleTime Infinity) — the JsonViewer
 * reads `$id`/`schema_ref` out of this document. `version` is the numeric version as a
 * string (the path converter).
 */
export function subjectVersionQueryOptions(wsId: string, subject: string, version: string) {
  return queryOptions({
    queryKey: queryKeys.schemaVersion(wsId, subject, version),
    staleTime: staleTimes.schemaVersion,
    queryFn: async (): Promise<VersionRecord> => {
      const { data, error } = await api.GET('/api/v1/schemas/{subject}/versions/{schema_version}', {
        params: { path: { subject, schema_version: version } },
      });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * `['w', id, 'schemas', subject, 'diff', from, to]` → the computed additive diff (#66).
 * Immutable (both versions are immutable, so the diff is too). `from`/`to` are version
 * numbers; the SubjectDetailPage drives this off adjacent timeline pairs. Passing
 * non-positive or out-of-order bounds is guarded by the caller (the API returns 400 on
 * `from >= to`); we use `skipToken` when no diff is requested.
 */
export function schemaDiffQueryOptions(
  wsId: string,
  subject: string,
  pair: { from: number; to: number } | null,
) {
  return queryOptions({
    queryKey: pair
      ? queryKeys.schemaDiff(wsId, subject, pair.from, pair.to)
      : queryKeys.schemaDiff(wsId, subject, 0, 0),
    staleTime: staleTimes.schemaVersion,
    queryFn: pair
      ? async (): Promise<SchemaDiff> => {
          const { data, error } = await api.GET('/api/v1/schemas/{subject}/diff', {
            params: { path: { subject }, query: { from: pair.from, to: pair.to } },
          });
          if (error) throw error as ApiError;
          return data;
        }
      : skipToken,
  });
}
