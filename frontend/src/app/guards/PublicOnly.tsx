import { Navigate, Outlet } from 'react-router';

import { useSession } from '../../features/auth';
import { PageSkeleton } from '../../shared/ui';

/**
 * PublicOnly (frontend-architecture §3.2). Authenticated visitors are bounced to
 * "/" (the WorkspaceResolver); waits for the bootstrap first so a logged-in user
 * reloading on /login does not flash the login form.
 */
export function PublicOnly() {
  const { isLoading, user } = useSession();
  if (isLoading) return <PageSkeleton />;
  if (user) return <Navigate to="/" replace />;
  return <Outlet />;
}
