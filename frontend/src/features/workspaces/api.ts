/**
 * Workspaces feature data layer (frontend-architecture §9.3). queryOptions +
 * mutation hooks for workspace CRUD, members, and the audit log. All transport,
 * keys, and invalidation come from `shared/api` (IMP-1).
 */
import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { invalidate } from '../../shared/api/invalidation';
import { ApiError } from '../../shared/api/problem';
import { queryKeys } from '../../shared/api/queryKeys';
import type {
  AuditEntry,
  Membership,
  RoleEnum,
  Workspace,
  WorkspaceCreate,
} from '../../shared/api/types';

/** `['workspaces']` → the user's workspaces (also drives the switcher; §4.2). */
export function workspacesQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.workspaces(),
    queryFn: async (): Promise<Workspace[]> => {
      const { data, error } = await api.GET('/api/v1/workspaces');
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/** `['w', id, 'members']` → membership rows (§9.3 MembersTable). */
export function membersQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.members(wsId),
    queryFn: async (): Promise<Membership[]> => {
      const { data, error } = await api.GET('/api/v1/workspaces/{workspace_id}/members', {
        params: { path: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/** `['w', id, 'activity']` → audit entries (§9.3 ActivityList; admin-only API). */
export function activityQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.activity(wsId),
    queryFn: async (): Promise<AuditEntry[]> => {
      const { data, error } = await api.GET('/api/v1/workspaces/{workspace_id}/audit-log', {
        params: { path: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

export function useCreateWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: WorkspaceCreate): Promise<Workspace> => {
      const { data, error } = await api.POST('/api/v1/workspaces', { body });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: async () => {
      await invalidate.onWorkspaceCreated(qc);
      // The new workspace is also a new membership → re-read the session.
      await qc.invalidateQueries({ queryKey: queryKeys.session() });
    },
  });
}

export function useDeleteWorkspace(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<void> => {
      const { error } = await api.DELETE('/api/v1/workspaces/{workspace_id}', {
        params: { path: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
    },
    onSuccess: async () => {
      await invalidate.onWorkspaceDeleted(qc, wsId);
      await qc.invalidateQueries({ queryKey: queryKeys.session() });
    },
  });
}

export function useInviteMember(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { email: string; role: RoleEnum }): Promise<Membership> => {
      const { data, error } = await api.POST('/api/v1/workspaces/{workspace_id}/members', {
        params: { path: { workspace_id: wsId } },
        body,
      });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onMembersChanged(qc, wsId),
  });
}

export function useUpdateMemberRole(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (args: { userId: string; role: RoleEnum }): Promise<Membership> => {
      const { data, error } = await api.PATCH(
        '/api/v1/workspaces/{workspace_id}/members/{user_id}',
        {
          params: { path: { workspace_id: wsId, user_id: args.userId } },
          body: { role: args.role },
        },
      );
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onMembersChanged(qc, wsId),
  });
}

export function useRemoveMember(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (args: { userId: string; isSelf: boolean }): Promise<void> => {
      const { error } = await api.DELETE(
        '/api/v1/workspaces/{workspace_id}/members/{user_id}',
        { params: { path: { workspace_id: wsId, user_id: args.userId } } },
      );
      if (error) throw error as ApiError;
    },
    onSuccess: (_data, args) => invalidate.onMembersChanged(qc, wsId, args.isSelf),
  });
}
