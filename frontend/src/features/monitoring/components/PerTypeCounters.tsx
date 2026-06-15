import { Skeleton } from '../../../shared/ui';

export interface PerTypeCountersProps {
  byEventType: Record<string, number> | undefined;
  isLoading?: boolean;
  /** Show at most this many types (top-N by count). */
  topN?: number;
}

/** CDC event types are namespaced `cdc.*` (event-model); everything else is business. */
function isCdc(type: string): boolean {
  return type.startsWith('cdc.') || type.startsWith('cdc_');
}

interface Counted {
  type: string;
  count: number;
}

/**
 * Top-N event types by count from the authoritative stats `by_event_type`
 * (frontend-architecture §9.7 `PerTypeCounters`), split into business vs CDC.
 */
export function PerTypeCounters({ byEventType, isLoading, topN = 12 }: PerTypeCountersProps) {
  if (isLoading) {
    return (
      <div className="rounded-lg border border-border bg-surface p-4">
        <Skeleton lines={4} className="h-5" />
      </div>
    );
  }

  const entries: Counted[] = Object.entries(byEventType ?? {})
    .map(([type, count]) => ({ type, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, topN);

  if (entries.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-surface p-4 text-sm text-text-muted">
        No events counted yet.
      </div>
    );
  }

  const business = entries.filter((e) => !isCdc(e.type));
  const cdc = entries.filter((e) => isCdc(e.type));

  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <h3 className="text-sm font-semibold text-text">Events by type</h3>
      <Group title="Business" items={business} />
      {cdc.length > 0 && <Group title="CDC" items={cdc} />}
    </div>
  );
}

function Group({ title, items }: { title: string; items: Counted[] }) {
  if (items.length === 0) return null;
  return (
    <div className="mt-3">
      <p className="text-xs font-medium uppercase tracking-wide text-text-muted">{title}</p>
      <ul className="mt-1.5 space-y-1">
        {items.map((e) => (
          <li key={e.type} className="flex items-center justify-between gap-3 text-sm">
            <span className="min-w-0 truncate font-mono text-xs text-text">{e.type}</span>
            <span className="tabular-nums text-text-muted">{e.count.toLocaleString('en-US')}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
