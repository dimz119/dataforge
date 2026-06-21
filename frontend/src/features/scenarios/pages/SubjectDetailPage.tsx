import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { ErrorState, NotFoundPage, PageHeader, PageSkeleton, Skeleton } from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import type { VersionProvenance } from '../../../shared/api/types';
import { schemaDiffQueryOptions, subjectQueryOptions } from '../api';
import { SchemaDiff } from '../components/SchemaDiff';
import { SchemaVersionViewer } from '../components/SchemaVersionViewer';

/** A subject is a CDC feed when its name carries the `.cdc.` segment (INV-REG-1). */
function isCdc(subject: string): boolean {
  return subject.includes('.cdc.');
}

/**
 * Subject detail (frontend-architecture §9.4; UI-4..6). The version timeline (newest
 * first) with each version's provenance badge — `manifest x.y.z` when the version was
 * derived from a manifest, `explicit` (Flow 2) when `manifest_version` is null — plus
 * `registered_at`. Selecting a version inspects its full document (JsonViewer); the
 * adjacent additive diff (vN-1 → vN) renders the added-field chips in green. The diff
 * for v1 (no predecessor) is omitted.
 */
export function SubjectDetailPage() {
  const ws = useActiveWorkspace();
  const { subject = '' } = useParams();
  const decoded = decodeURIComponent(subject);

  const detail = useQuery({
    ...subjectQueryOptions(ws?.workspaceId ?? '', decoded),
    enabled: Boolean(ws) && decoded !== '',
  });

  // Newest-first timeline; default selection = latest version.
  const timeline = useMemo<VersionProvenance[]>(() => {
    const rows = detail.data?.version_provenance ?? [];
    return [...rows].sort((a, b) => b.version - a.version);
  }, [detail.data]);

  const [selected, setSelected] = useState<number | null>(null);
  const activeVersion = selected ?? timeline[0]?.version ?? null;

  // Adjacent additive diff for the active version (vN-1 → vN); none for v1.
  const diffPair =
    activeVersion != null && activeVersion > 1
      ? { from: activeVersion - 1, to: activeVersion }
      : null;
  const diff = useQuery({
    ...schemaDiffQueryOptions(ws?.workspaceId ?? '', decoded, diffPair),
    enabled: Boolean(ws) && diffPair != null,
  });

  if (!ws) return <NotFoundPage />;
  if (detail.isPending) return <PageSkeleton />;
  if (detail.error) {
    return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />;
  }

  const basePath = `/w/${ws.slug}`;

  return (
    <div className="space-y-6">
      <PageHeader
        title={decoded}
        description={`${detail.data.scenario_slug} · ${detail.data.compatibility}`}
        actions={
          <Link
            to={`${basePath}/schemas`}
            className="inline-flex h-10 items-center rounded-md border border-border bg-surface px-4 text-sm font-medium text-text hover:bg-surface-muted"
          >
            All subjects
          </Link>
        }
      />

      <div className="grid gap-6 lg:grid-cols-[18rem_1fr]">
        {/* Version timeline (newest first). */}
        <ol aria-label="Version timeline" className="space-y-2">
          {timeline.map((v) => {
            const active = v.version === activeVersion;
            return (
              <li key={v.version}>
                <button
                  type="button"
                  onClick={() => setSelected(v.version)}
                  aria-pressed={active}
                  className={
                    active
                      ? 'w-full rounded-lg border border-accent bg-accent/10 p-3 text-left'
                      : 'w-full rounded-lg border border-border bg-surface p-3 text-left hover:bg-surface-muted'
                  }
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-sm font-semibold text-text">v{v.version}</span>
                    <span
                      className={
                        v.manifest_version == null
                          ? 'rounded bg-status-amber/15 px-1.5 py-0.5 text-[11px] font-medium text-status-amber'
                          : 'rounded bg-status-blue/15 px-1.5 py-0.5 text-[11px] font-medium text-status-blue'
                      }
                    >
                      {v.manifest_version == null ? 'explicit' : `manifest ${v.manifest_version}`}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-text-muted">
                    registered {formatRelativeTime(v.registered_at)}
                  </p>
                </button>
              </li>
            );
          })}
        </ol>

        {/* Detail column: the adjacent diff + the selected version document. */}
        <div className="space-y-4">
          {isCdc(decoded) && (
            <p
              role="note"
              className="rounded-md bg-status-blue/10 px-3 py-2 text-xs text-status-blue"
            >
              CDC subject — its row-image schema evolves only via a new manifest version
              (no Flow-2 upgrades, REG-U006).
            </p>
          )}

          {diffPair == null ? (
            <p className="rounded-lg border border-border bg-surface p-4 text-sm text-text-muted">
              v1 is the base version — no predecessor to diff against.
            </p>
          ) : diff.isPending ? (
            <Skeleton lines={3} className="h-8" />
          ) : diff.error ? (
            <ErrorState error={diff.error} onRetry={() => void diff.refetch()} />
          ) : diff.data ? (
            <SchemaDiff diff={diff.data} />
          ) : null}

          {activeVersion != null && (
            <SchemaVersionViewer
              workspaceId={ws.workspaceId}
              subject={decoded}
              version={activeVersion}
            />
          )}
        </div>
      </div>
    </div>
  );
}
