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
const StreamControlTab = lazy(() =>
  import('./pages/StreamControlTab').then((m) => ({ default: m.StreamControlTab })),
);

/**
 * Workspace-scoped stream routes, mounted under /w/:slug by app/router.tsx.
 *
 * `streams/:streamId` is a LAYOUT (the detail page header + tab bar + `<Outlet/>`):
 * the index child is the `control` tab; the Phase-9 `chaos` and `answer-key` tabs are
 * nested by the app router from `features/chaos` (features can't import each other,
 * IMP-2). `streamDetailExtraRoutes` lets the app inject those children here.
 */
export function buildStreamsRoutes(streamDetailExtraRoutes: RouteObject[] = []): RouteObject[] {
  return [
    { path: 'streams', element: <StreamListPage /> },
    { path: 'streams/new', element: <CreateStreamPage /> },
    {
      path: 'streams/:streamId',
      element: <StreamDetailPage />,
      children: [{ index: true, element: <StreamControlTab /> }, ...streamDetailExtraRoutes],
    },
  ];
}

/** Default streams routes (control tab only). The app may pass Phase-9 tab children. */
export const streamsRoutes: RouteObject[] = buildStreamsRoutes();
