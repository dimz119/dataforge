import { Navigate } from 'react-router';

import { useSession } from '../features/auth';
import { PageSkeleton } from '../shared/ui';

/**
 * "/" resolver (frontend-architecture §3.1). Redirects to the first membership's
 * dashboard, or to /workspaces/new when the user has none. Mounted under
 * RequireAuth, so `user` is present once the bootstrap settles.
 */
export function WorkspaceResolver() {
  const { isLoading, user } = useSession();
  if (isLoading) return <PageSkeleton />;
  const first = user?.memberships[0];
  if (!first) return <Navigate to="/workspaces/new" replace />;
  return <Navigate to={`/w/${first.slug}/dashboard`} replace />;
}
