import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import type { VersionSummary } from '../../../shared/api/types';
import {
  Button,
  EmptyState,
  ErrorState,
  NotFoundPage,
  PageHeader,
  PageSkeleton,
} from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import { instancesQueryOptions, scenarioQueryOptions } from '../api';
import { CreateInstanceDialog } from '../components/CreateInstanceDialog';

function VersionRow({ version }: { version: VersionSummary }) {
  const deprecated = version.status === 'deprecated';
  return (
    <li className="flex items-center justify-between px-4 py-3">
      <span className="flex items-center gap-2 font-mono text-sm text-text">
        v{version.manifest_version}
        {deprecated && (
          <span className="rounded bg-status-amber/15 px-1.5 py-0.5 text-[10px] font-medium uppercase text-status-amber">
            deprecated
          </span>
        )}
      </span>
      <time
        dateTime={version.published_at ?? undefined}
        className="text-xs text-text-muted"
        title={version.published_at ?? undefined}
      >
        {version.published_at ? formatRelativeTime(version.published_at) : 'unpublished'}
      </time>
    </li>
  );
}

/**
 * Scenario detail (frontend-architecture §9.4 ScenarioDetail). Published versions
 * list (deprecated chip per INV-CAT-5), instances of this scenario in the
 * workspace, and the create-instance flow (version picker defaults to latest).
 */
export function ScenarioDetailPage() {
  const ws = useActiveWorkspace();
  const { scenarioSlug = '' } = useParams();
  const [createOpen, setCreateOpen] = useState(false);

  const scenario = useQuery({
    ...scenarioQueryOptions(ws?.workspaceId ?? '', scenarioSlug),
    enabled: Boolean(ws) && scenarioSlug !== '',
  });
  const instances = useQuery({
    ...instancesQueryOptions(ws?.workspaceId ?? ''),
    enabled: Boolean(ws),
  });

  const scenarioInstances = useMemo(
    () => (instances.data ?? []).filter((i) => i.scenario_slug === scenarioSlug),
    [instances.data, scenarioSlug],
  );

  if (!ws) return <NotFoundPage />;
  if (scenario.isPending) return <PageSkeleton />;
  if (scenario.error) return <ErrorState error={scenario.error} onRetry={() => void scenario.refetch()} />;

  const detail = scenario.data;
  const canCreate = detail.published_versions.length > 0;
  const basePath = `/w/${ws.slug}`;

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <PageHeader
        title={detail.title}
        description={detail.description}
        actions={
          <Button onClick={() => setCreateOpen(true)} disabled={!canCreate}>
            Create instance
          </Button>
        }
      />

      <section aria-labelledby="versions-heading">
        <h2 id="versions-heading" className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
          Versions
        </h2>
        {detail.versions.length === 0 ? (
          <EmptyState title="No published versions" description="This scenario has no versions yet." />
        ) : (
          <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
            {detail.versions.map((v) => (
              <VersionRow key={v.manifest_version} version={v} />
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="instances-heading">
        <h2 id="instances-heading" className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
          Instances in this workspace
        </h2>
        {instances.isPending ? (
          <PageSkeleton />
        ) : scenarioInstances.length === 0 ? (
          <EmptyState
            title="No instances yet"
            description="Create an instance to configure this scenario and drive streams."
            action={
              <Button onClick={() => setCreateOpen(true)} disabled={!canCreate}>
                Create instance
              </Button>
            }
          />
        ) : (
          <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
            {scenarioInstances.map((i) => (
              <li key={i.scenario_instance_id} className="px-4 py-3">
                <Link
                  to={`${basePath}/scenarios/instances/${i.scenario_instance_id}`}
                  className="flex items-center justify-between text-sm hover:text-accent"
                >
                  <span className="font-medium text-text">{i.name}</span>
                  <span className="font-mono text-xs text-text-muted">
                    v{i.manifest_version} · rev {i.config_revision}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <CreateInstanceDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        workspaceId={ws.workspaceId}
        workspaceSlug={ws.slug}
        scenario={detail}
      />
    </div>
  );
}
