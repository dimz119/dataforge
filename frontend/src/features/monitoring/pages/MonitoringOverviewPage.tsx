import { useQueries, useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';

import {
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  PageHeader,
  StatusBadge,
  type Column,
} from '../../../shared/ui';
import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { isSettled } from '../../../shared/api/polling';
import { formatTps } from '../../../shared/lib/formatTps';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import type { StreamResponse, StreamStatsResponse } from '../../../shared/api/types';
import { streamStatsQueryOptions, streamsQueryOptions } from '../api';

interface Row {
  stream: StreamResponse;
  stats?: StreamStatsResponse;
}

const HEALTH_LABEL: Record<string, { label: string; tone: string }> = {
  healthy: { label: 'healthy', tone: 'text-status-green' },
  degraded: { label: 'degraded', tone: 'text-status-amber' },
  stale: { label: 'stale', tone: 'text-status-red' },
};

/**
 * Monitoring overview (frontend-architecture §9.7). A table of all streams with
 * StatusBadge, observed TPS, total events, last event, and a derived health cell;
 * row click → the stream monitor. Stats poll at 5 s (the authoritative source).
 */
export function MonitoringOverviewPage() {
  const ws = useActiveWorkspace();
  const wsId = ws?.workspaceId ?? '';
  const navigate = useNavigate();
  const streams = useQuery({ ...streamsQueryOptions(wsId), enabled: Boolean(wsId) });

  const list = streams.data ?? [];
  const statsResults = useQueries({
    queries: list.map((s) => streamStatsQueryOptions(wsId, s.stream_id, !isSettled(s.status))),
  });

  const rows: Row[] = list.map((stream, i) => ({ stream, stats: statsResults[i]?.data }));

  const columns: Column<Row>[] = [
    { id: 'name', header: 'Stream', cell: (r) => <span className="font-medium">{r.stream.name}</span> },
    { id: 'status', header: 'Status', cell: (r) => <StatusBadge status={r.stream.status} /> },
    {
      id: 'tps',
      header: 'Observed TPS',
      align: 'right',
      cell: (r) => <span className="tabular-nums">{r.stats ? formatTps(r.stats.observed_tps) : '—'}</span>,
    },
    {
      id: 'total',
      header: 'Total events',
      align: 'right',
      cell: (r) => (
        <span className="tabular-nums">
          {r.stats ? r.stats.total_events.toLocaleString('en-US') : '—'}
        </span>
      ),
    },
    {
      id: 'last',
      header: 'Last event',
      cell: (r) => <span className="text-text-muted">{formatRelativeTime(r.stats?.last_event_at)}</span>,
    },
    {
      id: 'health',
      header: 'Health',
      cell: (r) => {
        const h = r.stats?.health;
        if (!h) return <span className="text-text-muted">—</span>;
        const spec = HEALTH_LABEL[h] ?? { label: h, tone: 'text-text-muted' };
        return <span className={`text-xs font-medium ${spec.tone}`}>{spec.label}</span>;
      },
    },
  ];

  if (!ws) return null;
  if (streams.isError) {
    return (
      <div>
        <PageHeader title="Monitoring" />
        <ErrorState error={streams.error} onRetry={() => void streams.refetch()} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Monitoring" description="Live status across all streams in this workspace." />
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.stream.stream_id}
        isLoading={streams.isPending}
        onRowClick={(r) => void navigate(`/w/${ws.slug}/monitoring/${r.stream.stream_id}`)}
        caption="All streams with live status and observed throughput"
        empty={
          <EmptyState
            title="No streams yet"
            description="Start a stream to see live monitoring here."
            action={
              <Button onClick={() => void navigate(`/w/${ws.slug}/streams/new`)}>
                Start your first stream
              </Button>
            }
          />
        }
      />
    </div>
  );
}
