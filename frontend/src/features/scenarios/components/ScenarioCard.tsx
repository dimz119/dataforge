import { Link } from 'react-router';

import type { ScenarioSummary } from '../../../shared/api/types';

export interface ScenarioCardProps {
  scenario: ScenarioSummary;
  /** Base path of the current workspace, e.g. `/w/acme`. */
  basePath: string;
}

function VisibilityChip({ visibility }: { visibility: string }) {
  const isGlobal = visibility === 'global';
  return (
    <span
      className={
        isGlobal
          ? 'rounded-full bg-accent/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-accent'
          : 'rounded-full bg-surface-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-text-muted'
      }
    >
      {visibility}
    </span>
  );
}

/**
 * Scenario catalog card (frontend-architecture §9.4 CatalogGrid). Title,
 * description, visibility chip, latest version. Entity/event-type counts come
 * from the manifest and surface on the detail page (the summary does not carry
 * them). Click → scenario detail.
 */
export function ScenarioCard({ scenario, basePath }: ScenarioCardProps) {
  return (
    <Link
      to={`${basePath}/scenarios/${scenario.scenario_slug}`}
      className="flex flex-col rounded-lg border border-border bg-surface p-4 transition-colors hover:border-accent focus:outline-none focus-visible:border-accent"
    >
      <div className="flex items-start justify-between gap-2">
        <h2 className="font-semibold text-text">{scenario.title}</h2>
        <VisibilityChip visibility={scenario.visibility} />
      </div>
      <p className="mt-1 line-clamp-3 flex-1 text-sm text-text-muted">{scenario.description}</p>
      <p className="mt-3 text-xs text-text-muted">
        {scenario.latest_version
          ? `Latest v${scenario.latest_version} · ${String(scenario.published_versions.length)} published`
          : 'No published version'}
      </p>
    </Link>
  );
}
