import { useMemo, useState } from 'react';

import type { AuditEntry } from '../../../shared/api/types';
import { EmptyState, ErrorState, FormField, Skeleton } from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';

export interface ActivityListProps {
  entries: AuditEntry[];
  isLoading: boolean;
  error: unknown;
}

function actorLabel(actor: Record<string, unknown>): string {
  const email = actor['email'];
  if (typeof email === 'string') return email;
  const type = actor['type'];
  return typeof type === 'string' ? type : 'system';
}

/**
 * ActivityList (frontend-architecture §9.3). Audit entries `{action}` with actor
 * + relative time, filterable by action type. Export + advanced filtering arrive
 * in Phase 11; the list itself ships now over the Phase-2 audit API. (The audit
 * endpoint returns a flat list today; cursor infinite-scroll lands with Phase 11
 * pagination params — see frontend-architecture §9.3.)
 */
export function ActivityList({ entries, isLoading, error }: ActivityListProps) {
  const [filter, setFilter] = useState('');

  const actions = useMemo(
    () => Array.from(new Set(entries.map((e) => e.action))).sort(),
    [entries],
  );
  const visible = useMemo(
    () => (filter ? entries.filter((e) => e.action === filter) : entries),
    [entries, filter],
  );

  if (error) return <ErrorState error={error} />;
  if (isLoading) return <Skeleton lines={6} className="h-10" />;
  if (entries.length === 0) {
    return (
      <EmptyState
        title="No activity yet"
        description="Workspace actions — invites, key creation, stream lifecycle — appear here."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <FormField label="Filter by action" className="max-w-xs">
        {(p) => (
          <select
            id={p.id}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text"
          >
            <option value="">All actions</option>
            {actions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        )}
      </FormField>
      <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
        {visible.map((entry) => (
          <li key={entry.audit_id} className="flex items-baseline justify-between gap-4 px-4 py-3">
            <div className="min-w-0">
              <p className="truncate font-mono text-sm text-text">{entry.action}</p>
              <p className="text-xs text-text-muted">by {actorLabel(entry.actor)}</p>
            </div>
            <time
              dateTime={entry.occurred_at}
              className="shrink-0 text-xs text-text-muted"
              title={entry.occurred_at}
            >
              {formatRelativeTime(entry.occurred_at)}
            </time>
          </li>
        ))}
      </ul>
    </div>
  );
}
