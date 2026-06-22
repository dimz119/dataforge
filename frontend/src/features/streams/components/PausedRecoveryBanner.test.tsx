import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import type { StreamResponse } from '../../../shared/api/types';
import { PausedRecoveryBanner } from './PausedRecoveryBanner';

const mutate = vi.fn();
vi.mock('../api', () => ({
  useStreamLifecycle: () => ({ mutate, isPending: false }),
}));

function stream(status: string, status_reason = ''): StreamResponse {
  return {
    stream_id: 's-1',
    workspace_id: 'w-1',
    scenario_instance_id: 'i-1',
    name: 'orders',
    scenario_slug: 'ecommerce',
    manifest_version: '1.0.0',
    config_revision: 1,
    pin_sha256: 'x',
    seed: '1',
    status,
    status_reason,
    desired_state: { run_state: 'paused', target_tps: 10 },
    virtual_clock: { virtual_epoch: '2026-06-01T00:00:00Z', speed_multiplier: '1' },
    schema_versions: {},
    shard_count: 1,
    created_at: '2026-06-01T00:00:00Z',
    started_at: null,
    last_transition_at: null,
  };
}

describe('PausedRecoveryBanner', () => {
  it('renders nothing for a non-system-pause status', () => {
    renderWithProviders(
      <PausedRecoveryBanner workspaceId="w-1" stream={stream('running')} />,
    );
    expect(screen.queryByTestId('paused-recovery-banner')).toBeNull();
  });

  it('paused_quota: explains the guard and offers NO resume button', () => {
    renderWithProviders(
      <PausedRecoveryBanner workspaceId="w-1" stream={stream('paused_quota', 'quota')} />,
    );
    expect(screen.getByText(/quota exhausted/i)).toBeInTheDocument();
    expect(screen.getByTestId('quota-resume-guard')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
    expect(screen.getByText(/Reason:/)).toBeInTheDocument();
  });

  it('paused_idle: offers a one-click resume that issues the resume verb', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <PausedRecoveryBanner workspaceId="w-1" stream={stream('paused_idle', 'idle')} />,
    );
    const button = screen.getByRole('button', { name: 'Resume' });
    await user.click(button);
    expect(mutate).toHaveBeenCalledWith('resume', expect.anything());
  });
});
