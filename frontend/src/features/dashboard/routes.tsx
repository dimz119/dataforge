import { lazy } from 'react';
import type { RouteObject } from 'react-router';

// Lazy page chunk (frontend-architecture §3 / §12.2): the DashboardPage is
// code-split into its own per-page-group bundle so it loads only when /dashboard
// is visited. The Suspense fallback is the PageSkeleton wired into WorkspaceLayout.
const DashboardPage = lazy(() =>
  import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage })),
);

/** Workspace-scoped dashboard routes, mounted under /w/:slug by app/router.tsx. */
export const dashboardRoutes: RouteObject[] = [{ path: 'dashboard', element: <DashboardPage /> }];
