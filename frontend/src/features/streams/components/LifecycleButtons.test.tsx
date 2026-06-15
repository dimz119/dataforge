import { screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import { LifecycleButtons } from './LifecycleButtons';

// Mock the lifecycle mutation so the buttons render without a live transport.
vi.mock('../api', () => ({
  useStreamLifecycle: () => ({ mutate: vi.fn(), isPending: false, variables: undefined }),
}));

function render(status: string) {
  return renderWithProviders(
    <LifecycleButtons workspaceId="ws-1" streamId="s-1" status={status} />,
  );
}

describe('LifecycleButtons (matrix-driven, §9.5)', () => {
  it('running: Pause + Stop enabled; no Start/Resume', () => {
    render('running');
    expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  });

  it('paused: Resume + Stop enabled; no Pause', () => {
    render('paused');
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
  });

  it('paused_quota: Resume rendered but DISABLED with the T7 headroom tooltip', () => {
    render('paused_quota');
    const resume = screen.getByRole('button', { name: 'Resume' });
    expect(resume).toBeDisabled();
    expect(resume).toHaveAttribute('title', expect.stringContaining('headroom'));
    expect(screen.getByText(/headroom required/i)).toBeInTheDocument();
  });

  it('created: only Start (enabled); Stop hidden', () => {
    render('created');
    expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('starting: Start is pending (disabled+busy); Stop still available', () => {
    render('starting');
    const start = screen.getByRole('button', { name: 'Start' });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
  });

  it('failed: the start verb relabels to Retry (T13)', () => {
    render('failed');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
  });

  it('stopped: Start enabled with the continuation hint (T12)', () => {
    render('stopped');
    expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled();
    expect(screen.getByText(/continues from checkpoint/i)).toBeInTheDocument();
  });
});
