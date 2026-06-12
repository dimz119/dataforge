import { Outlet } from 'react-router';

/**
 * Phase 1 stub: always renders the outlet — Phase 7 replaces this.
 * The real guard suspends on the ['session'] bootstrap and redirects
 * unauthenticated visitors to /login with returnTo state
 * (frontend-architecture §3.2, §6.2).
 */
export function RequireAuth() {
  return <Outlet />;
}
