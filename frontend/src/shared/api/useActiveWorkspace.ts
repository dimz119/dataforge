/**
 * Active-workspace resolver shared across features (frontend-architecture §2.2,
 * §4.2). Features may not import each other (IMP boundary), so the workspace the
 * URL `:slug` resolves to is exposed from `shared/` instead of `features/auth`.
 *
 * It reads the already-populated `['session']` query cache (RequireAuth bootstraps
 * it before any workspace page mounts) and matches the route `:slug` against the
 * user's memberships — giving the tenant UUID (INV-TEN-1: query keys are keyed by
 * UUID, never slug) plus the caller's role.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';

import { queryKeys } from './queryKeys';
import type { MembershipSummary, UserMeResponse } from './types';

export interface ActiveWorkspace {
  /** Tenant UUID — the query-key prefix (INV-TEN-1). */
  workspaceId: string;
  slug: string;
  name: string;
  /** The current user's role in this workspace. */
  role: string;
  isAdmin: boolean;
}

/** Read the cached session user, or null when the bootstrap has not landed. */
export function useSessionUser(): UserMeResponse | null {
  const qc = useQueryClient();
  return qc.getQueryData<UserMeResponse>(queryKeys.session()) ?? null;
}

function toActive(m: MembershipSummary): ActiveWorkspace {
  return {
    workspaceId: m.workspace_id,
    slug: m.slug,
    name: m.name,
    role: m.role,
    isAdmin: m.role === 'admin',
  };
}

/**
 * The active workspace for the current `/w/:slug` route. Returns null only when
 * called outside a resolved workspace route (guards prevent this in practice).
 */
export function useActiveWorkspace(): ActiveWorkspace | null {
  const { slug } = useParams();
  const user = useSessionUser();
  if (!user || !slug) return null;
  const membership = user.memberships.find((m) => m.slug === slug);
  return membership ? toActive(membership) : null;
}
