import { Outlet } from 'react-router';

/**
 * Phase 1 stub: always renders the outlet — Phase 7 replaces this.
 * The real guard resolves :slug against the ['workspaces'] memberships and
 * renders NotFoundPage on miss, so cross-tenant probes are indistinguishable
 * from missing resources (frontend-architecture §3.2).
 */
export function RequireWorkspace() {
  return <Outlet />;
}
