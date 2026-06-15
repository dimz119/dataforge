import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { ErrorState, NotFoundPage, PageHeader, PageSkeleton } from '../../../shared/ui';
import { streamQueryOptions } from '../api';
import { StreamControlPanel } from '../components/StreamControlPanel';

/**
 * Stream detail / control page (frontend-architecture §9.5). Header with the live
 * StatusBadge, a tab bar that is CONTROL-ONLY for now (the `chaos` and `answer-key`
 * tabs are Phase 9), and the StreamControlPanel. The stream resource polls on a
 * status-keyed interval (§4.4): 2 s while converging, 10 s running, off when settled.
 */
export function StreamDetailPage() {
  const ws = useActiveWorkspace();
  const { streamId = '' } = useParams();

  const stream = useQuery({
    ...streamQueryOptions(ws?.workspaceId ?? '', streamId),
    enabled: Boolean(ws) && streamId !== '',
  });
  // Re-key the poll interval on the freshest status (§4.4 convergence).
  const live = useQuery({
    ...streamQueryOptions(ws?.workspaceId ?? '', streamId, stream.data?.status),
    enabled: Boolean(ws) && streamId !== '' && stream.data != null,
  });

  if (!ws) return <NotFoundPage />;
  if (stream.isPending) return <PageSkeleton />;
  if (stream.error) {
    return <ErrorState error={stream.error} onRetry={() => void stream.refetch()} />;
  }

  const data = live.data ?? stream.data;
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

      {/* Tab bar — control-only until Phase 9 adds the chaos + answer-key tabs. */}
      <nav aria-label="Stream sections" className="flex gap-1 border-b border-border">
        <span
          aria-current="page"
          className="border-b-2 border-accent px-3 py-2 text-sm font-medium text-text"
        >
          Control
        </span>
        {/* Phase 9: <Tab>Chaos</Tab> (live-mutable per PIN-3) */}
        {/* Phase 9: <Tab>Answer key</Tab> (RequireAdmin, ADR-0017) */}
      </nav>

      <StreamControlPanel workspaceId={ws.workspaceId} stream={data} />
    </div>
  );
}
