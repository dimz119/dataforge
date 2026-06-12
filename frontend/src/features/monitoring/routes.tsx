import type { RouteObject } from 'react-router';

import { MonitoringOverviewPage } from './pages/MonitoringOverviewPage';
import { StreamMonitorPage } from './pages/StreamMonitorPage';

/** Workspace-scoped monitoring routes, mounted under /w/:slug by app/router.tsx. */
export const monitoringRoutes: RouteObject[] = [
  { path: 'monitoring', element: <MonitoringOverviewPage /> },
  { path: 'monitoring/:streamId', element: <StreamMonitorPage /> },
];
