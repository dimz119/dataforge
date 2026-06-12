import type { RouteObject } from 'react-router';

import { DashboardPage } from './pages/DashboardPage';

/** Workspace-scoped dashboard routes, mounted under /w/:slug by app/router.tsx. */
export const dashboardRoutes: RouteObject[] = [{ path: 'dashboard', element: <DashboardPage /> }];
