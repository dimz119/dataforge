import { Navigate, Outlet, useLocation } from 'react-router';

import { useSession } from '../../features/auth';
import { PageSkeleton } from '../../shared/ui';

/**
 * RequireAuth (frontend-architecture §3.2, §6.2). Waits for the session bootstrap
 * (refresh → /users/me) so guarded pages never flash an unauthenticated state.
 * On no session, redirects to /login carrying `returnTo` so login can restore the
 * intended destination.
 */
export function RequireAuth() {
  const { isLoading, user } = useSession();
  const location = useLocation();

  if (isLoading) return <PageSkeleton />;
  if (!user) {
    const returnTo = `${location.pathname}${location.search}`;
    return <Navigate to="/login" replace state={{ returnTo }} />;
  }
  return <Outlet />;
}
