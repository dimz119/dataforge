import { lazy } from 'react';
import type { RouteObject } from 'react-router';

// Lazy page chunk (frontend-architecture §3 / §12.2): the ApiKeysPage is
// code-split into its own per-page-group bundle so it loads only when /api-keys
// is visited. The Suspense fallback is the PageSkeleton wired into WorkspaceLayout.
const ApiKeysPage = lazy(() =>
  import('./pages/ApiKeysPage').then((m) => ({ default: m.ApiKeysPage })),
);

/** Workspace-scoped API-key routes, mounted under /w/:slug by app/router.tsx. */
export const apikeysRoutes: RouteObject[] = [{ path: 'api-keys', element: <ApiKeysPage /> }];
