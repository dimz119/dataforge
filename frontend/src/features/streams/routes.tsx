import { lazy } from 'react';
import type { RouteObject } from 'react-router';

// Lazy page chunks (frontend-architecture §3 / §12.2): each streams page is
// code-split into its own per-page-group bundle so it loads only when the matching
// path is visited. The Suspense fallback is the PageSkeleton wired into WorkspaceLayout.
const StreamListPage = lazy(() =>
  import('./pages/StreamListPage').then((m) => ({ default: m.StreamListPage })),
);
const CreateStreamPage = lazy(() =>
  import('./pages/CreateStreamPage').then((m) => ({ default: m.CreateStreamPage })),
);
const StreamDetailPage = lazy(() =>
  import('./pages/StreamDetailPage').then((m) => ({ default: m.StreamDetailPage })),
);

/** Workspace-scoped stream routes, mounted under /w/:slug by app/router.tsx. */
export const streamsRoutes: RouteObject[] = [
  { path: 'streams', element: <StreamListPage /> },
  { path: 'streams/new', element: <CreateStreamPage /> },
  { path: 'streams/:streamId', element: <StreamDetailPage /> },
];
