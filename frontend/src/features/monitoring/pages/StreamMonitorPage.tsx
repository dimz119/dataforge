import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useParams } from 'react-router';

import {
  ErrorState,
  NotFoundPage,
  PageHeader,
  PageSkeleton,
  StatusBadge,
} from '../../../shared/ui';
import { ApiError } from '../../../shared/api/problem';
import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { formatTps } from '../../../shared/lib/formatTps';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import { isSettled } from '../../../shared/api/polling';
import { LiveTail } from '../components/LiveTail';
import { PerTypeCounters } from '../components/PerTypeCounters';
import { streamQueryOptions, streamStatsQueryOptions } from '../api';

/**
 * Single-stream monitor (frontend-architecture §9.7). Header reads the stream + its
 * authoritative stats (5 s poll); `LiveTail` (the WS hook §7.6) renders below.
 * Stats totals are labelled "stream totals"; the tail counters are "this connection"
 * — distinct sources (INV-OBS-2). Phase 9 adds the chaos + answer-key tabs.
 */
export function StreamMonitorPage() {
  const ws = useActiveWorkspace();
  const wsId = ws?.workspaceId ?? '';
  const { streamId = '' } = useParams();

  const stream = useQuery({
    ...streamQueryOptions(wsId, streamId),
    enabled: Boolean(wsId && streamId),
  });
  const status = stream.data?.status;
  const stats = useQuery({
    ...streamStatsQueryOptions(wsId, streamId, Boolean(status) && !isSettled(status)),
    enabled: Boolean(wsId && streamId),
  });

  const knownTypes = useMemo(
    () => Object.keys(stats.data?.by_event_type ?? {}).sort(),
    [stats.data],
  );

  if (!ws) return null;

  // Cross-tenant masking / missing → NotFound presentation (§10.1).
  if (stream.error instanceof ApiError && (stream.error.status === 404 || stream.error.status === 403)) {
    return <NotFoundPage />;
  }
  if (stream.isPending) return <PageSkeleton />;
  if (stream.isError) {
    return (
      <div>
        <PageHeader title="Stream monitor" />
        <ErrorState error={stream.error} onRetry={() => void stream.refetch()} />
      </div>
    );
  }

  const s = stream.data;

  return (
    <div className="space-y-5">
      <PageHeader
        title={s.name}
        description={`Stream monitor · ${s.scenario_slug} @ ${s.manifest_version}`}
        actions={<StatusBadge status={s.status} />}
      />

      <section className="grid grid-cols-2 gap-4 sm:grid-cols-4" aria-label="Stream totals">
        <Metric label="Observed TPS" value={stats.data ? formatTps(stats.data.observed_tps) : '—'} note="stream totals" />
        <Metric
          label="Total events"
          value={stats.data ? stats.data.total_events.toLocaleString('en-US') : '—'}
        />
        <Metric label="Target TPS" value={stats.data ? stats.data.target_tps.toLocaleString('en-US') : '—'} />
        <Metric label="Last event" value={formatRelativeTime(stats.data?.last_event_at)} />
      </section>

      <div className="grid gap-5 lg:grid-cols-[1fr_18rem]">
        <LiveTail streamId={streamId} streamStatus={s.status} knownTypes={knownTypes} />
        <PerTypeCounters byEventType={stats.data?.by_event_type} isLoading={stats.isPending} />
      </div>
    </div>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-text-muted">{label}</p>
      <p className="mt-1 text-xl font-semibold tabular-nums text-text">{value}</p>
      {note && <p className="mt-0.5 text-[10px] uppercase tracking-wide text-text-muted">{note}</p>}
    </div>
  );
}
