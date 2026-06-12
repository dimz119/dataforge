import { createBrowserRouter, type RouteObject } from 'react-router';

import { authPublicOnlyRoutes, authTokenRoutes } from '../features/auth';
import { apikeysRoutes } from '../features/apikeys';
import { dashboardRoutes } from '../features/dashboard';
import { monitoringRoutes } from '../features/monitoring';
import { scenariosRoutes } from '../features/scenarios';
import { streamsRoutes } from '../features/streams';
import { workspacesAdminRoutes, workspacesAuthRoutes } from '../features/workspaces';
import { NotFoundPage } from '../shared/ui/NotFoundPage';
import { PlaceholderPage } from '../shared/ui/PlaceholderPage';
import { PublicOnly } from './guards/PublicOnly';
import { RequireAdmin } from './guards/RequireAdmin';
import { RequireAuth } from './guards/RequireAuth';
import { RequireWorkspace } from './guards/RequireWorkspace';
import { AuthLayout } from './layouts/AuthLayout';
import { WorkspaceLayout } from './layouts/WorkspaceLayout';

/**
 * Phase 1 stub: placeholder page — Phase 7 replaces this with the real
 * WorkspaceResolver, which redirects "/" to /w/{last-or-first slug}/dashboard,
 * or to /workspaces/new when the user has no workspace
 * (frontend-architecture §3.1).
 */
function WorkspaceResolverPage() {
  return <PlaceholderPage group="Console shell" page="Workspace resolver" phase={7} />;
}

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
      { path: '/', element: <WorkspaceResolverPage /> },
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
