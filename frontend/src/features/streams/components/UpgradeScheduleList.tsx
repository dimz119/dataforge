import { useEffect, useState } from 'react';

import { ConfirmDialog, StatusBadge } from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import type { SchemaUpgradeResponse } from '../../../shared/api/types';
import { useCancelSchemaUpgrade } from '../api';
import {
  cutoverCountdown,
  formatDuration,
  type VirtualClockSample,
} from '../simulatedTime';

export interface UpgradeScheduleListProps {
  workspaceId: string;
  streamId: string;
  /** The full upgrade history (scheduled, applied, AND cancelled — §10.3). */
  upgrades: SchemaUpgradeResponse[];
  /** The stream's virtual-clock sample for the simulated-time countdown. */
  clock: VirtualClockSample;
}

/** Map an upgrade status onto a StatusBadge tone string. */
const STATUS_LABEL: Record<string, string> = {
  scheduled: 'starting', // blue/pulse — pending cutover
  applied: 'running', // green — cut over
  cancelled: 'stopped', // gray
};

/**
 * The scheduled / applied / cancelled upgrade timeline (frontend-architecture §9.5).
 * Each `scheduled` entry shows a SIMULATED-TIME countdown derived from the stream's
 * virtual clock (NEVER wall time) — the cutover fires when the virtual clock reaches
 * `at`, so the panel also estimates the wall ETA from the live speed multiplier. `applied`
 * entries surface the per-shard `applied_sequence_no` + the wall instant the cutover
 * landed (§10.4); `cancelled` entries are retained as history. Only `scheduled` entries
 * are cancellable (else 409 invalid-state-transition).
 */
export function UpgradeScheduleList({
  workspaceId,
  streamId,
  upgrades,
  clock,
}: UpgradeScheduleListProps) {
  const cancel = useCancelSchemaUpgrade(workspaceId, streamId);
  const [confirmId, setConfirmId] = useState<string | null>(null);

  // Tick once a second so the simulated-time countdown advances between polls.
  const [, setNow] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setNow((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (upgrades.length === 0) {
    return (
      <p className="text-sm text-text-muted">No upgrades scheduled for this stream.</p>
    );
  }

  return (
    <>
      <ul className="space-y-2">
        {upgrades.map((u) => (
          <li
            key={u.upgrade_id}
            className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface p-3"
          >
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <code className="font-mono text-sm text-text">{u.subject}</code>
                <span className="text-xs text-text-muted">→ v{u.target_version}</span>
                <StatusBadge status={STATUS_LABEL[u.status] ?? 'created'} />
              </div>
              <p className="text-xs text-text-muted">{describe(u, clock)}</p>
            </div>
            {u.status === 'scheduled' && (
              <button
                type="button"
                onClick={() => setConfirmId(u.upgrade_id)}
                className="shrink-0 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-text hover:bg-surface-muted"
              >
                Cancel
              </button>
            )}
          </li>
        ))}
      </ul>

      <ConfirmDialog
        open={confirmId != null}
        onOpenChange={(open) => {
          if (!open) setConfirmId(null);
        }}
        title="Cancel scheduled upgrade?"
        description="The pending cutover will not fire. Cancelled upgrades are retained in the history."
        confirmLabel="Cancel upgrade"
        cancelLabel="Keep"
        danger
        loading={cancel.isPending}
        onConfirm={() => {
          if (confirmId) cancel.mutate(confirmId);
          setConfirmId(null);
        }}
      />
    </>
  );
}

/** The status-specific descriptive line under each upgrade row. */
function describe(u: SchemaUpgradeResponse, clock: VirtualClockSample): string {
  if (u.status === 'applied') {
    const seq = u.applied_sequence_no != null ? ` at seq ${String(u.applied_sequence_no)}` : '';
    return `Applied ${formatRelativeTime(u.applied_at_wall)}${seq}.`;
  }
  if (u.status === 'cancelled') {
    return `Cancelled ${formatRelativeTime(u.cancelled_at)}.`;
  }
  // scheduled — simulated-time countdown (never wall time).
  if (!u.at) return 'Cuts over on the next tick.';
  const cd = cutoverCountdown(u.at, clock);
  if (Number.isNaN(cd.simulatedMsRemaining)) {
    return 'Cuts over once the stream starts (clock not yet running).';
  }
  if (cd.due) return 'Cutover imminent (simulated time reached).';
  const wall = cd.wallMsRemaining != null ? ` (~${formatDuration(cd.wallMsRemaining)} wall)` : '';
  return `Cuts over in ${formatDuration(cd.simulatedMsRemaining)} simulated${wall}.`;
}
