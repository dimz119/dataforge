/**
 * The invalidation matrix as code (frontend-architecture §4.3). Each mutation
 * hook calls the matching helper in its `onSuccess`; the matrix stays in ONE
 * reviewed place rather than being re-derived per feature (§5.2 rationale).
 *
 * "invalidate" = `invalidateQueries`; "remove" = `removeQueries`.
 */
import type { QueryClient } from '@tanstack/react-query';

import { queryKeys } from './queryKeys';

export const invalidate = {
  /** logout: drop ALL cached data — every tenant subtree leaves memory (§4.3, §6.4). */
  onLogout(qc: QueryClient): void {
    qc.clear();
  },

  /** verifyEmail / resetPassword → re-read the session. */
  async onSession(qc: QueryClient): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.session() });
  },

  /** createWorkspace → refresh the membership list. */
  async onWorkspaceCreated(qc: QueryClient): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.workspaces() });
  },

  /** updateWorkspace (name/slug) → list + detail. */
  async onWorkspaceUpdated(qc: QueryClient, wsId: string): Promise<void> {
    await Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.workspaces() }),
      qc.invalidateQueries({ queryKey: queryKeys.workspaceDetail(wsId) }),
    ]);
  },

  /** deleteWorkspace → list invalidate + remove the whole tenant subtree. */
  async onWorkspaceDeleted(qc: QueryClient, wsId: string): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.workspaces() });
    qc.removeQueries({ queryKey: queryKeys.workspace(wsId) });
  },

  /** invite/remove member, change role → members (+ workspaces when removing self). */
  async onMembersChanged(qc: QueryClient, wsId: string, removedSelf = false): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.members(wsId) });
    if (removedSelf) await qc.invalidateQueries({ queryKey: queryKeys.workspaces() });
  },

  /** createApiKey / revokeApiKey → keys list. Plaintext NEVER enters the cache (§9.6). */
  async onKeysChanged(qc: QueryClient, wsId: string): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.keys(wsId) });
  },

  /** createInstance → instances list. */
  async onInstancesChanged(qc: QueryClient, wsId: string): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.instances(wsId) });
  },

  /** updateInstanceConfig → the one instance (bumps config_revision; PIN-2: no stream keys). */
  async onInstanceConfigSaved(qc: QueryClient, wsId: string, instanceId: string): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.instance(wsId, instanceId) });
  },

  /** createStream → stream list. */
  async onStreamCreated(qc: QueryClient, wsId: string): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.streams(wsId) });
  },

  /** start/pause/resume/stop/setTargetTps → stream detail + list (convergence then settles). */
  async onStreamLifecycle(qc: QueryClient, wsId: string, streamId: string): Promise<void> {
    await Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.stream(wsId, streamId) }),
      qc.invalidateQueries({ queryKey: queryKeys.streams(wsId) }),
    ]);
  },

  /** deleteStream → list invalidate + remove the stream subtree. */
  async onStreamDeleted(qc: QueryClient, wsId: string, streamId: string): Promise<void> {
    await qc.invalidateQueries({ queryKey: queryKeys.streams(wsId) });
    qc.removeQueries({ queryKey: queryKeys.stream(wsId, streamId) });
  },

  /** updateChaosPolicy (Phase 9) → chaos + stream detail. */
  async onChaosUpdated(qc: QueryClient, wsId: string, streamId: string): Promise<void> {
    await Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.streamChaos(wsId, streamId) }),
      qc.invalidateQueries({ queryKey: queryKeys.stream(wsId, streamId) }),
    ]);
  },
} as const;
