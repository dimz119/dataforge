import { Outlet, useParams } from 'react-router';

import { findMembership, useSession } from '../../features/auth';
import { NotFoundPage, PageSkeleton } from '../../shared/ui';

/**
 * RequireAdmin (frontend-architecture §3.2, ADR-0017). Requires the membership
 * role for `:slug` to be `admin`; renders NotFoundPage on failure (route guard).
 * For TAB-level admin gating (e.g. the answer-key tab), features hide the tab
 * instead of mounting this guard.
 */
export function RequireAdmin() {
  const { isLoading, user } = useSession();
  const { slug } = useParams();

  if (isLoading) return <PageSkeleton />;
  const membership = findMembership(user, slug);
  if (!membership || membership.role !== 'admin') return <NotFoundPage />;
  return <Outlet />;
}
