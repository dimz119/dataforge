import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';

import { ErrorState, Skeleton } from '../../../shared/ui';
import type { StreamResponse } from '../../../shared/api/types';
import {
  streamSchemaUpgradesQueryOptions,
  streamSchemaVersionsQueryOptions,
  subjectsQueryOptions,
} from '../api';
import type { VirtualClockSample } from '../simulatedTime';
import { ScheduleUpgradeForm } from './ScheduleUpgradeForm';
import { UpgradeScheduleList } from './UpgradeScheduleList';

export interface SchemaPanelProps {
  workspaceId: string;
  /** Active workspace slug, for the registry-browser links. */
  slug: string;
  stream: StreamResponse;
}

/**
 * The stream-detail schema panel (frontend-architecture §9.5; Phase 10). Surfaces:
 *  - the §10.2 effective-version map (`schema_versions` on the Stream resource),
 *  - the scheduled / applied / cancelled upgrade timeline with a SIMULATED-TIME
 *    countdown computed from the stream's virtual clock (never wall time), and
 *  - the scheduling form (business subjects with a higher registered version; REG-U*
 *    failures rendered inline from the problem `errors[]`).
 *
 * The schema-versions projection polls on the same status-keyed interval as the stream
 * so the pending → applied cutover is observed live. Before first start the effective map
 * is a preview; it is materialized and frozen at start (PIN-R1).
 */
export function SchemaPanel({ workspaceId, slug, stream }: SchemaPanelProps) {
  const streamId = stream.stream_id;
  const versions = useQuery(
    streamSchemaVersionsQueryOptions(workspaceId, streamId, stream.status),
  );
  const upgrades = useQuery(streamSchemaUpgradesQueryOptions(workspaceId, streamId));
  const subjects = useQuery(subjectsQueryOptions(workspaceId));

  // The effective map: prefer the authoritative §10.2 projection, fall back to the
  // Stream resource's additive `schema_versions` field (the same effective map).
  const effective = versions.data?.effective ?? stream.schema_versions;

  // Virtual-clock sample for the simulated-time countdown (sampled at render time).
  const clock: VirtualClockSample = {
    virtualNowIso: stream.virtual_clock.virtual_now ?? null,
    speedMultiplier: Number(stream.virtual_clock.speed_multiplier) || 1,
    sampledAtMs: Date.now(),
  };

  return (
    <section
      aria-labelledby="schema-heading"
      className="space-y-5 rounded-lg border border-border bg-surface p-5"
    >
      <div>
        <h2 id="schema-heading" className="text-sm font-semibold text-text">
          Schema versions
        </h2>
        <p className="mt-0.5 text-xs text-text-muted">
          Effective versions and scheduled mid-stream evolutions (cutover at simulated
          time, §10).
        </p>
      </div>

      {/* Effective-version map. */}
      <div>
        <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-text-muted">
          Effective
        </h3>
        {Object.keys(effective).length === 0 ? (
          <p className="text-sm text-text-muted">
            Pinned at first start (PIN-R1) — start the stream to materialize the effective
            map.
          </p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {Object.entries(effective).map(([subject, version]) => (
              <li
                key={subject}
                className="flex items-center gap-1.5 rounded-md border border-border bg-surface-muted px-2.5 py-1 text-xs"
              >
                <Link
                  to={`/w/${slug}/schemas/${encodeURIComponent(subject)}`}
                  className="font-mono text-text hover:text-accent"
                >
                  {subject}
                </Link>
                <span className="font-mono font-semibold text-accent">v{version}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Upgrade timeline. */}
      <div className="border-t border-border pt-4">
        <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-text-muted">
          Upgrades
        </h3>
        {upgrades.error ? (
          <ErrorState error={upgrades.error} onRetry={() => void upgrades.refetch()} />
        ) : upgrades.isPending ? (
          <Skeleton lines={2} className="h-10" />
        ) : (
          <UpgradeScheduleList
            workspaceId={workspaceId}
            streamId={streamId}
            upgrades={upgrades.data}
            clock={clock}
          />
        )}
      </div>

      {/* Scheduling form. */}
      <div className="border-t border-border pt-4">
        <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-text-muted">
          Schedule an upgrade
        </h3>
        {subjects.error ? (
          <ErrorState error={subjects.error} onRetry={() => void subjects.refetch()} />
        ) : subjects.isPending ? (
          <Skeleton lines={3} className="h-9" />
        ) : (
          <ScheduleUpgradeForm
            workspaceId={workspaceId}
            streamId={streamId}
            subjects={subjects.data}
            effective={effective}
          />
        )}
      </div>
    </section>
  );
}
