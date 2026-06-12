import type { RouteObject } from 'react-router';

import { CreateWorkspacePage } from './pages/CreateWorkspacePage';
import { SettingsPage } from './pages/SettingsPage';

/** Mounted behind RequireAuth at the root level by app/router.tsx (§3.1). */
export const workspacesAuthRoutes: RouteObject[] = [
  { path: '/workspaces/new', element: <CreateWorkspacePage /> },
];

/** Workspace-scoped admin routes, mounted under /w/:slug behind RequireAdmin. */
export const workspacesAdminRoutes: RouteObject[] = [
  { path: 'settings', element: <SettingsPage /> },
];
