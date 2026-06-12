import { Outlet } from 'react-router';

/**
 * Phase 1 stub: always renders the outlet — Phase 7 replaces this.
 * The real guard requires the membership role to be `admin` and renders
 * NotFoundPage on failure (frontend-architecture §3.2, ADR-0017).
 */
export function RequireAdmin() {
  return <Outlet />;
}
