/**
 * Session hooks consumed by guards and the shell (frontend-architecture §3.2,
 * §6.2). The `['session']` query's queryFn IS the bootstrap (§6.2): refresh via
 * the df_refresh cookie, then read /users/me. It returns `null` (not throw) when
 * unauthenticated, so guards branch on data rather than error.
 */
import { useQuery } from '@tanstack/react-query';

import { queryKeys, staleTimes } from '../../shared/api/queryKeys';
import type { MembershipSummary, UserMeResponse } from '../../shared/api/types';
import { bootstrapSession } from './api';

export interface SessionState {
  /** True until the §6.2 bootstrap settles — guards suspend their decision on this. */
  isLoading: boolean;
  /** The authenticated user, or null when unauthenticated. */
  user: UserMeResponse | null;
}

/**
 * The session query. One bootstrap per app load (cached for 5 min); guards read
 * the cache, never fetch imperatively (§3.2). React Query dedupes the in-flight
 * promise so the four guards mounting at once share one bootstrap.
 */
export function useSession(): SessionState {
  const query = useQuery({
    queryKey: queryKeys.session(),
    queryFn: bootstrapSession,
    staleTime: staleTimes.session,
    retry: false,
    refetchOnWindowFocus: false,
  });
  return { isLoading: query.isLoading, user: query.data ?? null };
}

/** Resolve a workspace `:slug` to the user's membership, or null (RequireWorkspace). */
export function findMembership(
  user: UserMeResponse | null,
  slug: string | undefined,
): MembershipSummary | null {
  if (!user || !slug) return null;
  return user.memberships.find((m) => m.slug === slug) ?? null;
}
