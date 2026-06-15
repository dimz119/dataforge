import { useQueries, useQuery } from '@tanstack/react-query';

import {
  EmptyState,
  ErrorState,
  PageHeader,
  PageSkeleton,
} from '../../../shared/ui';
import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { isSettled } from '../../../shared/api/polling';
import type { StreamResponse } from '../../../shared/api/types';
import {
  streamStatsQueryOptions,
  streamsQueryOptions,
  workspaceDetailQueryOptions,
} from '../api';
import { GettingStartedPanel } from '../components/GettingStartedPanel';
import { StreamStatsCard } from '../components/StreamStatsCard';
import { WorkspaceSummaryCard } from '../components/WorkspaceSummaryCard';

/** Top streams shown as stats cards (§9.2: top 6 by `last_event_at`). */
const TOP_N = 6;

/** Recency sort: most-recent `last_event_at` first, never-fired streams last. */
function byRecency(a: StreamResponse, b: StreamResponse): number {
  const at = a.last_transition_at ?? a.created_at;
  const bt = b.last_transition_at ?? b.created_at;
  return bt.localeCompare(at);
}

/**
 * Dashboard (frontend-architecture §9.2). Workspace summary (usage numbers, NO limit
 * bars — Phase 11), the top-6 stream stats cards (5 s poll + sparkline), and the
 * getting-started panel when the workspace has no streams.
 */
export function DashboardPage() {
  const ws = useActiveWorkspace();
  const wsId = ws?.workspaceId ?? '';
  const workspace = useQuery({ ...workspaceDetailQueryOptions(wsId), enabled: Boolean(wsId) });
  const streams = useQuery({ ...streamsQueryOptions(wsId), enabled: Boolean(wsId) });

  const topStreams = (streams.data ?? []).slice().sort(byRecency).slice(0, TOP_N);

  // Sum events-today from the same stats queries the cards drive (cache-shared).
  const statsResults = useQueries({
    queries: topStreams.map((s) =>
      streamStatsQueryOptions(wsId, s.stream_id, !isSettled(s.status)),
    ),
  });
  const eventsToday = statsResults.reduce((sum, q) => sum + (q.data?.total_events ?? 0), 0);

  if (!ws) return <PageSkeleton />;
  if (streams.isError) {
    return (
      <div>
        <PageHeader title="Dashboard" description={ws.name} />
        <ErrorState error={streams.error} onRetry={() => void streams.refetch()} />
      </div>
    );
  }

  const hasStreams = (streams.data?.length ?? 0) > 0;

  return (
    <div className="space-y-6">
      <PageHeader title="Dashboard" description={ws.name} />

      <WorkspaceSummaryCard
        workspace={workspace.data}
        streams={streams.data ?? []}
        eventsToday={eventsToday}
        isLoading={workspace.isPending || streams.isPending}
      />

      {!hasStreams && !streams.isPending ? (
        <GettingStartedPanel slug={ws.slug} />
      ) : (
        <section aria-labelledby="streams-heading">
          <h2 id="streams-heading" className="mb-3 text-sm font-semibold text-text">
            Streams
          </h2>
          {streams.isPending ? (
            <PageSkeleton />
          ) : topStreams.length === 0 ? (
            <EmptyState title="No streams yet" />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {topStreams.map((stream) => (
                <StreamStatsCard
                  key={stream.stream_id}
                  wsId={wsId}
                  slug={ws.slug}
                  stream={stream}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
