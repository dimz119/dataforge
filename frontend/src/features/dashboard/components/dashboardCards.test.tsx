import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';

import type { StreamResponse, Workspace } from '../../../shared/api/types';
import { GettingStartedPanel } from './GettingStartedPanel';
import { WorkspaceSummaryCard } from './WorkspaceSummaryCard';

const workspace: Workspace = {
  workspace_id: 'w1',
  name: 'Acme',
  slug: 'acme',
  plan: 'classroom',
  member_count: 3,
  created_at: '2026-06-01T00:00:00Z',
};

function stream(status: string): StreamResponse {
  return {
    stream_id: `s-${status}`,
    workspace_id: 'w1',
    scenario_instance_id: 'i1',
    name: `stream ${status}`,
    scenario_slug: 'ecommerce',
    manifest_version: '1.0.0',
    config_revision: 1,
    pin_sha256: 'x',
    seed: '1',
    status,
    status_reason: '',
    desired_state: { run_state: 'running', target_tps: 10 },
    virtual_clock: { virtual_epoch: '2026-06-01T00:00:00Z', speed_multiplier: '1' },
    shard_count: 1,
    created_at: '2026-06-01T00:00:00Z',
    started_at: null,
    last_transition_at: null,
  };
}

describe('WorkspaceSummaryCard', () => {
  it('shows usage numbers without limit bars (Phase 11 deferred)', () => {
    render(
      <WorkspaceSummaryCard
        workspace={workspace}
        streams={[stream('running'), stream('stopped')]}
        eventsToday={1234}
      />,
    );
    expect(screen.getByText('classroom')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument(); // member count
    expect(screen.getByText('1,234')).toBeInTheDocument(); // events today
    expect(screen.getByText('1')).toBeInTheDocument(); // active streams (only running)
    expect(screen.queryByRole('progressbar')).toBeNull(); // no limit bars
  });
});

describe('GettingStartedPanel', () => {
  it('renders the 4-step path with workspace deep links', () => {
    render(
      <MemoryRouter>
        <GettingStartedPanel slug="acme" />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: /Browse scenarios/ })).toHaveAttribute(
      'href',
      '/w/acme/scenarios',
    );
    expect(screen.getByRole('link', { name: /Create a key/ })).toHaveAttribute(
      'href',
      '/w/acme/api-keys',
    );
    expect(screen.getByRole('link', { name: /Start a stream/ })).toHaveAttribute(
      'href',
      '/w/acme/streams/new',
    );
    expect(screen.getByRole('link', { name: /Open monitoring/ })).toHaveAttribute(
      'href',
      '/w/acme/monitoring',
    );
  });
});
