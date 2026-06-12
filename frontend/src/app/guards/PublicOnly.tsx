import { Outlet } from 'react-router';

/**
 * Phase 1 stub: always renders the outlet — Phase 7 replaces this.
 * The real guard navigates authenticated sessions to "/" replace
 * (frontend-architecture §3.2).
 */
export function PublicOnly() {
  return <Outlet />;
}
