import { lazy } from 'react';
import type { RouteObject } from 'react-router';

// Lazy tab chunks (frontend-architecture §12.2): the chaos + answer-key panels load
// only when their stream-detail tab is visited, keeping each in its own route chunk
// under the 150 KB budget (§12.1). Composed as children of the streams detail layout
// by app/router.tsx — features cannot import each other (IMP-2), so the app wires them.
const ChaosTab = lazy(() =>
  import('./pages/ChaosTab').then((m) => ({ default: m.ChaosTab })),
);
const AnswerKeyTab = lazy(() =>
  import('./pages/AnswerKeyTab').then((m) => ({ default: m.AnswerKeyTab })),
);

/** Child routes nested under `streams/:streamId` (the StreamDetailPage layout). */
export const chaosTabRoutes: RouteObject[] = [
  { path: 'chaos', element: <ChaosTab /> },
  { path: 'answer-key', element: <AnswerKeyTab /> },
];
