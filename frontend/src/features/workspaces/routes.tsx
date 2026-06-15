import { lazy, Suspense } from 'react';
import type { RouteObject } from 'react-router';

import { PageSkeleton } from '../../shared/ui';

// Lazy page chunks (frontend-architecture §3 / §12.2): each workspaces page is
// code-split into its own per-page-group bundle so it loads only when the matching
// path is visited.
//
// SettingsPage mounts under WorkspaceLayout, whose Suspense boundary supplies the
// PageSkeleton fallback. CreateWorkspacePage mounts at the root level (/workspaces/new)
// outside any layout, so its lazy element carries its own Suspense fallback here.
const CreateWorkspacePage = lazy(() =>
  import('./pages/CreateWorkspacePage').then((m) => ({ default: m.CreateWorkspacePage })),
);
const SettingsPage = lazy(() =>
  import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })),
);

/** Mounted behind RequireAuth at the root level by app/router.tsx (§3.1). */
export const workspacesAuthRoutes: RouteObject[] = [
  {
    path: '/workspaces/new',
    element: (
      <Suspense fallback={<PageSkeleton />}>
        <CreateWorkspacePage />
      </Suspense>
    ),
  },
];

/** Workspace-scoped admin routes, mounted under /w/:slug behind RequireAdmin. */
export const workspacesAdminRoutes: RouteObject[] = [{ path: 'settings', element: <SettingsPage /> }];
