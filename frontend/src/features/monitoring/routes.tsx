import { lazy } from 'react';
import type { RouteObject } from 'react-router';

// Lazy page chunks (frontend-architecture §3 / §12.2): each monitoring page is
// code-split into its own per-page-group bundle so it loads only when the matching
// path is visited. The Suspense fallback is the PageSkeleton wired into WorkspaceLayout.
const MonitoringOverviewPage = lazy(() =>
  import('./pages/MonitoringOverviewPage').then((m) => ({ default: m.MonitoringOverviewPage })),
);
const StreamMonitorPage = lazy(() =>
  import('./pages/StreamMonitorPage').then((m) => ({ default: m.StreamMonitorPage })),
);

/** Workspace-scoped monitoring routes, mounted under /w/:slug by app/router.tsx. */
export const monitoringRoutes: RouteObject[] = [
  { path: 'monitoring', element: <MonitoringOverviewPage /> },
  { path: 'monitoring/:streamId', element: <StreamMonitorPage /> },
];
