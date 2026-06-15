import { useQuery } from '@tanstack/react-query';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { EmptyState, ErrorState, NotFoundPage, PageHeader, Skeleton } from '../../../shared/ui';
import { scenariosQueryOptions } from '../api';
import { ScenarioCard } from '../components/ScenarioCard';

/**
 * Scenario catalog (frontend-architecture §9.4 CatalogGrid). A responsive grid of
 * scenario cards. The registry browser (Phase 10) is a separate surface and is
 * not rendered here.
 */
export function CatalogPage() {
  const ws = useActiveWorkspace();
  const scenarios = useQuery({
    ...scenariosQueryOptions(ws?.workspaceId ?? ''),
    enabled: Boolean(ws),
  });

  if (!ws) return <NotFoundPage />;

  const basePath = `/w/${ws.slug}`;

  return (
    <div>
      <PageHeader
        title="Scenarios"
        description="Pick a scenario, then create a configured instance to drive streams."
      />

      {scenarios.error ? (
        <ErrorState error={scenarios.error} onRetry={() => void scenarios.refetch()} />
      ) : scenarios.isPending ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-32" />
          ))}
        </div>
      ) : scenarios.data.length === 0 ? (
        <EmptyState
          title="No scenarios available"
          description="Scenarios published to your workspace will appear here."
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {scenarios.data.map((s) => (
            <ScenarioCard key={s.scenario_slug} scenario={s} basePath={basePath} />
          ))}
        </div>
      )}
    </div>
  );
}
