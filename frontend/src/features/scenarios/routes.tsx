import type { RouteObject } from 'react-router';

import { CatalogPage } from './pages/CatalogPage';
import { InstanceConfigPage } from './pages/InstanceConfigPage';
import { ScenarioDetailPage } from './pages/ScenarioDetailPage';

/**
 * Workspace-scoped scenario routes, mounted under /w/:slug by app/router.tsx.
 * The two /schemas registry-browser routes appear in Phase 10 (§3.1) — absent here.
 */
export const scenariosRoutes: RouteObject[] = [
  { path: 'scenarios', element: <CatalogPage /> },
  { path: 'scenarios/:scenarioSlug', element: <ScenarioDetailPage /> },
  { path: 'scenarios/instances/:instanceId', element: <InstanceConfigPage /> },
];
