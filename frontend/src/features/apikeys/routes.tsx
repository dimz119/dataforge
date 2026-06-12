import type { RouteObject } from 'react-router';

import { ApiKeysPage } from './pages/ApiKeysPage';

/** Workspace-scoped API-key routes, mounted under /w/:slug by app/router.tsx. */
export const apikeysRoutes: RouteObject[] = [{ path: 'api-keys', element: <ApiKeysPage /> }];
