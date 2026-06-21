import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import {
  DataTable,
  EmptyState,
  ErrorState,
  Input,
  NotFoundPage,
  PageHeader,
  type Column,
} from '../../../shared/ui';
import type { SubjectSummary } from '../../../shared/api/types';
import { subjectsQueryOptions } from '../api';

/** A subject is a CDC feed when its name carries the `.cdc.` segment (INV-REG-1). */
function isCdc(subject: string): boolean {
  return subject.includes('.cdc.');
}

/**
 * Schema registry browser (frontend-architecture §3.1/§9.4; UI-1..3). The subjects
 * table — name, owning scenario, latest version, version count, the BACKWARD_ADDITIVE
 * compatibility chip, and a business/cdc badge — with a scenario filter and a name
 * search. Rows link through to the SubjectDetailPage version timeline. The list is
 * session-stable (5-min staleTime); subjects never mutate within a session beyond a new
 * version being appended.
 */
export function RegistryBrowserPage() {
  const ws = useActiveWorkspace();
  const navigate = useNavigate();
  const subjects = useQuery({
    ...subjectsQueryOptions(ws?.workspaceId ?? ''),
    enabled: Boolean(ws),
  });

  const [search, setSearch] = useState('');
  const [scenario, setScenario] = useState('');

  const scenarioOptions = useMemo(() => {
    const set = new Set((subjects.data ?? []).map((s) => s.scenario_slug));
    return Array.from(set).sort();
  }, [subjects.data]);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (subjects.data ?? []).filter(
      (s) =>
        (scenario === '' || s.scenario_slug === scenario) &&
        (q === '' || s.subject.toLowerCase().includes(q)),
    );
  }, [subjects.data, search, scenario]);

  if (!ws) return <NotFoundPage />;

  const basePath = `/w/${ws.slug}`;

  const columns: Column<SubjectSummary>[] = [
    {
      id: 'subject',
      header: 'Subject',
      cell: (s) => <span className="font-mono text-sm text-text">{s.subject}</span>,
    },
    {
      id: 'kind',
      header: 'Kind',
      cell: (s) =>
        isCdc(s.subject) ? (
          <span className="rounded bg-status-blue/15 px-1.5 py-0.5 text-[11px] font-medium text-status-blue">
            cdc
          </span>
        ) : (
          <span className="rounded bg-status-gray/15 px-1.5 py-0.5 text-[11px] font-medium text-status-gray">
            business
          </span>
        ),
    },
    { id: 'scenario', header: 'Scenario', cell: (s) => s.scenario_slug },
    {
      id: 'compatibility',
      header: 'Compatibility',
      cell: (s) => (
        <span className="rounded bg-status-green/10 px-1.5 py-0.5 text-[11px] font-medium text-status-green">
          {s.compatibility}
        </span>
      ),
    },
    {
      id: 'latest',
      header: 'Latest',
      align: 'right',
      cell: (s) => (
        <span className="font-mono tabular-nums">
          {s.latest_version == null ? '—' : `v${String(s.latest_version)}`}
        </span>
      ),
    },
    {
      id: 'count',
      header: 'Versions',
      align: 'right',
      cell: (s) => <span className="font-mono tabular-nums">{s.versions.length}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="Schemas"
        description="Browse registered subjects, their version history, and additive evolutions."
      />

      {subjects.error ? (
        <ErrorState error={subjects.error} onRetry={() => void subjects.refetch()} />
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <Input
              type="search"
              placeholder="Search subjects…"
              aria-label="Search subjects"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="max-w-xs"
            />
            <select
              aria-label="Filter by scenario"
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
              className="h-10 rounded-md border border-border bg-surface px-3 text-sm text-text"
            >
              <option value="">All scenarios</option>
              {scenarioOptions.map((slug) => (
                <option key={slug} value={slug}>
                  {slug}
                </option>
              ))}
            </select>
          </div>

          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(s) => s.subject}
            isLoading={subjects.isPending}
            caption="Registered schema subjects"
            onRowClick={(s) => {
              void navigate(`${basePath}/schemas/${encodeURIComponent(s.subject)}`);
            }}
            empty={
              <EmptyState
                title="No subjects found"
                description={
                  search || scenario
                    ? 'No subjects match the current filters.'
                    : 'Registered schema subjects will appear here once scenarios are synced.'
                }
              />
            }
          />
        </div>
      )}
    </div>
  );
}
