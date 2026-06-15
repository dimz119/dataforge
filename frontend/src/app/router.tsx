import { createBrowserRouter, type RouteObject } from 'react-router';

import { authPublicOnlyRoutes, authTokenRoutes } from '../features/auth';
import { apikeysRoutes } from '../features/apikeys';
import { dashboardRoutes } from '../features/dashboard';
import { monitoringRoutes } from '../features/monitoring';
import { scenariosRoutes } from '../features/scenarios';
import { streamsRoutes } from '../features/streams';
import { workspacesAdminRoutes, workspacesAuthRoutes } from '../features/workspaces';
import { NotFoundPage } from '../shared/ui/NotFoundPage';
import { PublicOnly } from './guards/PublicOnly';
import { RequireAdmin } from './guards/RequireAdmin';
import { RequireAuth } from './guards/RequireAuth';
import { RequireWorkspace } from './guards/RequireWorkspace';
import { AuthLayout } from './layouts/AuthLayout';
import { WorkspaceLayout } from './layouts/WorkspaceLayout';
import { WorkspaceResolver } from './WorkspaceResolver';

/**
 * The full Phase 1 routing skeleton: every Phase-7 path of the routing map
 * (frontend-architecture §3.1) registered with placeholder pages behind
 * stubbed guards. The two /schemas registry-browser routes appear in Phase 10;
 * the /w/:slug/channels group appears in Phase 12 — both absent here by spec.
 */
export const routes: RouteObject[] = [
  {
    element: <RequireAuth />,
    children: [
      { path: '/', element: <WorkspaceResolver /> },
      ...workspacesAuthRoutes,
      {
        path: '/w/:slug',
        element: <RequireWorkspace />,
        children: [
          {
            element: <WorkspaceLayout />,
            children: [
              ...dashboardRoutes,
              ...scenariosRoutes,
              ...streamsRoutes,
              ...apikeysRoutes,
              ...monitoringRoutes,
              { element: <RequireAdmin />, children: [...workspacesAdminRoutes] },
            ],
          },
        ],
      },
    ],
  },
  {
    element: <PublicOnly />,
    children: [{ element: <AuthLayout />, children: [...authPublicOnlyRoutes] }],
  },
  { element: <AuthLayout />, children: [...authTokenRoutes] },
  { path: '*', element: <NotFoundPage /> },
];

export function createAppRouter() {
  return createBrowserRouter(routes);
}
