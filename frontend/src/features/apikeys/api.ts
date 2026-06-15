/**
 * API-keys feature data layer (frontend-architecture §9.6). The plaintext key
 * appears ONLY in the create mutation's result and is handed straight to the
 * reveal-once dialog's local state — it is NEVER written into the Query cache
 * (INV-TEN-4; §4.3 createApiKey row). The list query returns prefix+last4 only.
 */
import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '../../shared/api/client';
import { invalidate } from '../../shared/api/invalidation';
import { ApiError } from '../../shared/api/problem';
import { queryKeys } from '../../shared/api/queryKeys';
import type { ApiKeyCreate, ApiKeyCreated, ApiKeyListItem } from '../../shared/api/types';

/** `['w', id, 'keys']` → list items (prefix+last4 only; never the secret; §9.6). */
export function apiKeysQueryOptions(wsId: string) {
  return queryOptions({
    queryKey: queryKeys.keys(wsId),
    queryFn: async (): Promise<ApiKeyListItem[]> => {
      const { data, error } = await api.GET('/api/v1/workspaces/{workspace_id}/api-keys', {
        params: { path: { workspace_id: wsId } },
      });
      if (error) throw error as ApiError;
      return data.data;
    },
  });
}

/**
 * Create a key. The 201 carries the plaintext `key` exactly once. `onSuccess`
 * invalidates the list but the resolved value (with the secret) is returned to
 * the caller for the reveal-once dialog — it never enters the cache.
 */
export function useCreateApiKey(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ApiKeyCreate): Promise<ApiKeyCreated> => {
      const { data, error } = await api.POST('/api/v1/workspaces/{workspace_id}/api-keys', {
        params: { path: { workspace_id: wsId } },
        body,
      });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onKeysChanged(qc, wsId),
  });
}

export function useRevokeApiKey(wsId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (apiKeyId: string): Promise<void> => {
      const { error } = await api.DELETE(
        '/api/v1/workspaces/{workspace_id}/api-keys/{api_key_id}',
        { params: { path: { workspace_id: wsId, api_key_id: apiKeyId } } },
      );
      if (error) throw error as ApiError;
    },
    onSuccess: () => invalidate.onKeysChanged(qc, wsId),
  });
}
