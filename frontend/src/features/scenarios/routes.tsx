import { lazy } from 'react';
import type { RouteObject } from 'react-router';

// Lazy page chunks (frontend-architecture §3 / §12.2): each scenarios page is
// code-split into its own per-page-group bundle so it loads only when the matching
// path is visited. The Suspense fallback is the PageSkeleton wired into WorkspaceLayout.
const CatalogPage = lazy(() =>
  import('./pages/CatalogPage').then((m) => ({ default: m.CatalogPage })),
);
const ScenarioDetailPage = lazy(() =>
  import('./pages/ScenarioDetailPage').then((m) => ({ default: m.ScenarioDetailPage })),
);
const InstanceConfigPage = lazy(() =>
  import('./pages/InstanceConfigPage').then((m) => ({ default: m.InstanceConfigPage })),
);

/**
 * Workspace-scoped scenario routes, mounted under /w/:slug by app/router.tsx.
 * The two /schemas registry-browser routes appear in Phase 10 (§3.1) — absent here.
 */
export const scenariosRoutes: RouteObject[] = [
  { path: 'scenarios', element: <CatalogPage /> },
  { path: 'scenarios/:scenarioSlug', element: <ScenarioDetailPage /> },
  { path: 'scenarios/instances/:instanceId', element: <InstanceConfigPage /> },
];
