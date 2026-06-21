import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useId, useState } from 'react';

import {
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Input,
  useToast,
  type Column,
} from '../../../shared/ui';
import type { AnswerKeyInjection } from '../../../shared/api/types';
import {
  answerKeyInjectionsOptions,
  answerKeySummaryOptions,
  downloadAnswerKeyJsonl,
  flattenInjections,
  type AnswerKeyFilters,
} from '../api';
import { CHAOS_MODES, MODE_META } from '../types';

export interface AnswerKeyPanelProps {
  workspaceId: string;
  streamId: string;
  /** Admin role OR the `answer_key:read` scope (ADR-0017). */
  canRead: boolean;
}

const COLUMNS: Column<AnswerKeyInjection>[] = [
  { id: 'mode', header: 'Mode', cell: (r) => MODE_META[r.mode as keyof typeof MODE_META]?.label ?? r.mode },
  {
    id: 'event_id',
    header: 'Event id',
    cell: (r) => <span className="font-mono text-xs">{r.event_id.slice(0, 8)}…</span>,
  },
  { id: 'seq', header: 'Seq', align: 'right', cell: (r) => r.sequence_no },
  {
    id: 'canonical',
    header: 'Canonical (configured)',
    cell: (r) => <span className="text-xs">{new Date(r.canonical_emitted_at).toLocaleString()}</span>,
  },
  {
    id: 'occurred',
    header: 'Occurred (realized)',
    cell: (r) => <span className="text-xs">{new Date(r.occurred_at).toLocaleString()}</span>,
  },
];

/**
 * AnswerKeyPanel (frontend-architecture §9.5; ADR-0017) — the `answer-key` tab. Gated
 * on admin/answer_key:read: renders a requires-scope empty state otherwise. Shows the
 * per-mode summary counts, a cursor-paginated injection list with mode/time filters,
 * and a "Download JSONL" export. Ground truth never rides in delivered events (INV-DEL-2).
 */
export function AnswerKeyPanel({ workspaceId, streamId, canRead }: AnswerKeyPanelProps) {
  const toast = useToast();
  const [filters, setFilters] = useState<AnswerKeyFilters>({});
  const [downloading, setDownloading] = useState(false);
  const modeId = useId();
  const fromId = useId();
  const toId = useId();

  const summary = useQuery({
    ...answerKeySummaryOptions(workspaceId, streamId, filters),
    enabled: canRead,
  });
  const injections = useInfiniteQuery({
    ...answerKeyInjectionsOptions(workspaceId, streamId, filters),
    enabled: canRead,
  });

  if (!canRead) {
    return (
      <EmptyState
        title="Answer key is restricted"
        description="Requires admin or the answer_key:read scope (ADR-0017). The answer key holds delivery ground truth and is never exposed to graded consumers."
      />
    );
  }

  const setFilter = (patch: Partial<AnswerKeyFilters>) =>
    setFilters((f) => ({ ...f, ...patch }));

  const rows = injections.data ? flattenInjections(injections.data.pages) : [];

  const onDownload = () => {
    setDownloading(true);
    downloadAnswerKeyJsonl(streamId, filters)
      .catch((err) => toast.showError(err, 'Could not export answer key'))
      .finally(() => setDownloading(false));
  };

  return (
    <div className="space-y-5">
      <p className="rounded-md border border-border bg-surface-muted px-3 py-2 text-xs text-text-muted">
        Ground truth is never present in delivered events (INV-DEL-2) — graders read it here.
      </p>

      <section className="rounded-lg border border-border bg-surface p-4">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-semibold text-text">Injections by mode</h3>
          <span className="text-sm text-text-muted">
            {summary.data ? `${summary.data.total_injections} total` : '—'}
          </span>
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {CHAOS_MODES.map((mode) => {
            const count = (summary.data?.by_mode as Record<string, number> | undefined)?.[mode] ?? 0;
            return (
              <div key={mode} className="rounded-md bg-surface-muted px-3 py-2">
                <dt className="text-[11px] text-text-muted">{MODE_META[mode].label}</dt>
                <dd className="font-mono text-sm text-text">{count}</dd>
              </div>
            );
          })}
        </dl>
      </section>

      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <label htmlFor={modeId} className="text-xs font-medium text-text">
            Mode
          </label>
          <select
            id={modeId}
            value={filters.mode ?? ''}
            onChange={(e) => setFilter({ mode: e.target.value || undefined })}
            className="h-10 rounded-md border border-border bg-surface px-3 text-sm text-text focus:outline-none"
          >
            <option value="">All modes</option>
            {CHAOS_MODES.map((m) => (
              <option key={m} value={m}>
                {MODE_META[m].label}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={fromId} className="text-xs font-medium text-text">
            From
          </label>
          <Input
            id={fromId}
            type="datetime-local"
            className="w-auto"
            onChange={(e) =>
              setFilter({ from: e.target.value ? new Date(e.target.value).toISOString() : undefined })
            }
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={toId} className="text-xs font-medium text-text">
            To
          </label>
          <Input
            id={toId}
            type="datetime-local"
            className="w-auto"
            onChange={(e) =>
              setFilter({ to: e.target.value ? new Date(e.target.value).toISOString() : undefined })
            }
          />
        </div>
        <Button variant="secondary" onClick={onDownload} loading={downloading}>
          Download JSONL
        </Button>
      </div>

      {injections.error ? (
        <ErrorState error={injections.error} onRetry={() => void injections.refetch()} />
      ) : (
        <DataTable
          columns={COLUMNS}
          rows={rows}
          rowKey={(r) => r.injection_id}
          isLoading={injections.isPending}
          caption="Recorded chaos injections (answer key)"
          empty={
            <EmptyState
              title="No injections recorded"
              description="Enable chaos modes and run the stream to record delivery deviations."
            />
          }
        />
      )}

      {injections.hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="ghost"
            onClick={() => void injections.fetchNextPage()}
            loading={injections.isFetchingNextPage}
          >
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
