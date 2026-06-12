import type { RouteObject } from 'react-router';

import { CreateStreamPage } from './pages/CreateStreamPage';
import { StreamDetailPage } from './pages/StreamDetailPage';
import { StreamListPage } from './pages/StreamListPage';

/** Workspace-scoped stream routes, mounted under /w/:slug by app/router.tsx. */
export const streamsRoutes: RouteObject[] = [
  { path: 'streams', element: <StreamListPage /> },
  { path: 'streams/new', element: <CreateStreamPage /> },
  { path: 'streams/:streamId', element: <StreamDetailPage /> },
];
