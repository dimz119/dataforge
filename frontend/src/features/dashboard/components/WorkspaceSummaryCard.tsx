import { Skeleton } from '../../../shared/ui';
import type { StreamResponse, Workspace } from '../../../shared/api/types';

export interface WorkspaceSummaryCardProps {
  workspace?: Workspace;
  streams: StreamResponse[];
  /** Events today, summed from per-stream stats (`total_events`). */
  eventsToday: number;
  isLoading?: boolean;
}

/** A live, transitional-aware "active" status set for the active-streams count. */
const ACTIVE_STATUSES = new Set([
  'starting',
  'running',
  'resuming',
  'pausing',
  'paused',
  'paused_quota',
  'paused_idle',
  'stopping',
]);

interface StatProps {
  label: string;
  value: string;
  hint?: string;
}

function Stat({ label, value, hint }: StatProps) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-text-muted">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums text-text">{value}</dd>
      {hint && <p className="mt-0.5 text-xs text-text-muted">{hint}</p>}
    </div>
  );
}

/**
 * Workspace summary (frontend-architecture §9.2). Shows member count, plan tier,
 * events today, and active streams as USAGE NUMBERS WITHOUT LIMIT BARS — the
 * `QuotaMeter` bars (events/day, aggregate TPS vs caps) land in Phase 11 once quota
 * enforcement exists.
 */
export function WorkspaceSummaryCard({
  workspace,
  streams,
  eventsToday,
  isLoading,
}: WorkspaceSummaryCardProps) {
  if (isLoading || !workspace) {
    return (
      <section className="rounded-lg border border-border bg-surface p-5">
        <Skeleton lines={3} className="h-8" />
      </section>
    );
  }
  const activeStreams = streams.filter((s) => ACTIVE_STATUSES.has(s.status)).length;

  return (
    <section
      aria-labelledby="ws-summary-heading"
      className="rounded-lg border border-border bg-surface p-5"
    >
      <h2 id="ws-summary-heading" className="sr-only">
        Workspace summary
      </h2>
      <dl className="grid grid-cols-2 gap-5 sm:grid-cols-4">
        <Stat label="Plan" value={workspace.plan} hint="usage shown; limits in Phase 11" />
        <Stat
          label="Members"
          value={workspace.member_count.toLocaleString('en-US')}
        />
        <Stat label="Events today" value={eventsToday.toLocaleString('en-US')} />
        <Stat
          label="Active streams"
          value={activeStreams.toLocaleString('en-US')}
          hint={`${streams.length.toLocaleString('en-US')} total`}
        />
      </dl>
      {/* Phase 11: QuotaMeter bars (events/day, aggregate TPS vs caps) render here. */}
    </section>
  );
}
