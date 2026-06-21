import { useQuery } from '@tanstack/react-query';
import { Suspense } from 'react';
import { Link, NavLink, Outlet, useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { ErrorState, NotFoundPage, PageHeader, PageSkeleton } from '../../../shared/ui';
import { streamQueryOptions } from '../api';

/** Tab definitions for the stream-detail tab bar (control + Phase-9 chaos/answer-key). */
const TABS = [
  { to: '', label: 'Control', end: true },
  { to: 'chaos', label: 'Chaos', end: false },
  { to: 'answer-key', label: 'Answer key', end: false },
] as const;

/**
 * Stream detail / control page (frontend-architecture §9.5). Header with the live
 * StatusBadge title, a tab bar (Phase 9 adds the `chaos` and `answer-key` tabs to the
 * previously control-only bar), and an `<Outlet/>` for the active tab. The stream
 * resource polls on a status-keyed interval (§4.4): 2 s converging, 10 s running, off
 * when settled. Child tabs re-read the same query (cache shared by key).
 */
export function StreamDetailPage() {
  const ws = useActiveWorkspace();
  const { streamId = '' } = useParams();

  const stream = useQuery({
    ...streamQueryOptions(ws?.workspaceId ?? '', streamId),
    enabled: Boolean(ws) && streamId !== '',
  });
  // Re-key the poll interval on the freshest status (§4.4 convergence).
  useQuery({
    ...streamQueryOptions(ws?.workspaceId ?? '', streamId, stream.data?.status),
    enabled: Boolean(ws) && streamId !== '' && stream.data != null,
  });

  if (!ws) return <NotFoundPage />;
  if (stream.isPending) return <PageSkeleton />;
  if (stream.error) {
    return <ErrorState error={stream.error} onRetry={() => void stream.refetch()} />;
  }

  const data = stream.data;
  const basePath = `/w/${ws.slug}`;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        title={data.name}
        description={`${data.scenario_slug}@${data.manifest_version}`}
        actions={
          <Link
            to={`${basePath}/monitoring/${data.stream_id}`}
            className="inline-flex h-10 items-center rounded-md border border-border bg-surface px-4 text-sm font-medium text-text hover:bg-surface-muted"
          >
            Live tail
          </Link>
        }
      />

      {/* Tab bar — Phase 9 added the chaos + answer-key tabs (PIN-3 / ADR-0017). */}
      <nav aria-label="Stream sections" className="flex gap-1 border-b border-border">
        {TABS.map((tab) => (
          <NavLink
            key={tab.label}
            to={tab.to}
            end={tab.end}
            className={({ isActive }) =>
              `px-3 py-2 text-sm font-medium ${
                isActive
                  ? 'border-b-2 border-accent text-text'
                  : 'border-b-2 border-transparent text-text-muted hover:text-text'
              }`
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>

      <Suspense fallback={<PageSkeleton />}>
        <Outlet />
      </Suspense>
    </div>
  );
}
