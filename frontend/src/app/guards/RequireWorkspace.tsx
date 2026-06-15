import { Outlet, useParams } from 'react-router';

import { findMembership, useSession } from '../../features/auth';
import { NotFoundPage, PageSkeleton } from '../../shared/ui';

/**
 * RequireWorkspace (frontend-architecture §3.2). Resolves `:slug` against the
 * session's memberships. A miss renders NotFoundPage — cross-tenant probes are
 * INDISTINGUISHABLE from missing resources, mirroring the API's 403/404 policy.
 */
export function RequireWorkspace() {
  const { isLoading, user } = useSession();
  const { slug } = useParams();

  if (isLoading) return <PageSkeleton />;
  if (!findMembership(user, slug)) return <NotFoundPage />;
  return <Outlet />;
}
