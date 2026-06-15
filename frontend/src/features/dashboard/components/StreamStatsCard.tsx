import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';

import { Skeleton, Sparkline, StatusBadge } from '../../../shared/ui';
import { formatTps } from '../../../shared/lib/formatTps';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import type { StreamResponse } from '../../../shared/api/types';
import { isSettled } from '../../../shared/api/polling';
import { streamStatsQueryOptions } from '../api';

export interface StreamStatsCardProps {
  wsId: string;
  slug: string;
  stream: StreamResponse;
}

/** Observed-TPS history kept ~15 min at the 5 s stats poll (180 samples). */
const MAX_SAMPLES = 180;

/**
 * One stream's live stats (frontend-architecture §9.2). Polls stats every 5 s
 * (INV-OBS-2) and accumulates an observed-TPS sparkline. Settled streams skip the
 * poll. The whole card links to the stream's monitor.
 */
export function StreamStatsCard({ wsId, slug, stream }: StreamStatsCardProps) {
  const polls = !isSettled(stream.status);
  const stats = useQuery(streamStatsQueryOptions(wsId, stream.stream_id, polls));
  const [history, setHistory] = useState<number[]>([]);
  const lastAsOf = useRef<string | null>(null);

  useEffect(() => {
    const data = stats.data;
    if (!data || data.as_of === lastAsOf.current) return;
    lastAsOf.current = data.as_of;
    setHistory((prev) => [...prev, data.observed_tps].slice(-MAX_SAMPLES));
  }, [stats.data]);

  return (
    <Link
      to={`/w/${slug}/monitoring/${stream.stream_id}`}
      className="block rounded-lg border border-border bg-surface p-4 transition-colors hover:border-border-strong focus-visible:border-border-strong focus-visible:outline-none"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="min-w-0 truncate text-sm font-semibold text-text">{stream.name}</h3>
        <StatusBadge status={stream.status} />
      </div>
      <div className="mt-3 flex items-end justify-between gap-3">
        <div>
          <p className="text-2xl font-semibold tabular-nums text-text">
            {stats.data ? formatTps(stats.data.observed_tps) : '—'}
          </p>
          <p className="mt-0.5 text-xs text-text-muted">
            {stats.data ? `${stats.data.total_events.toLocaleString('en-US')} events` : ' '}
          </p>
        </div>
        {history.length >= 2 ? (
          <Sparkline values={history} label={`${stream.name} observed TPS, last 15 minutes`} />
        ) : stats.isPending ? (
          <Skeleton className="h-8 w-[120px]" />
        ) : (
          <Sparkline values={history} />
        )}
      </div>
      <p className="mt-2 text-xs text-text-muted">
        last event {formatRelativeTime(stats.data?.last_event_at)}
      </p>
    </Link>
  );
}
