import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import type { ChaosPolicyResponse } from '../../../shared/api/types';
import { ChaosPanel } from './ChaosPanel';
import type { ChaosPolicyDocument } from '../types';

const mutate = vi.fn<(_doc: ChaosPolicyDocument) => void>();

// A clean policy: every mode disabled, discard on stop (chaos-engine §3.2 shape).
const cleanModes: Record<string, unknown> = {
  duplicates: { enabled: false, rate: 0.05, params: {} },
  late_arriving: { enabled: false, rate: 0.03, params: {} },
  missing: { enabled: false, rate: 0.01, params: {} },
  out_of_order: { enabled: false, rate: 0.1, params: {} },
  corrupted_values: { enabled: false, rate: 0.02, params: {} },
  nulls: { enabled: false, rate: 0.02, params: {} },
  schema_drift: { enabled: false, rate: 0.2, params: {} },
  on_stop_policy: 'discard',
};

const response: ChaosPolicyResponse = {
  stream_id: 's-1',
  modes: cleanModes,
  updated_at: '2026-06-14T10:00:00Z',
};

vi.mock('../api', () => ({
  chaosQueryOptions: () => ({ queryKey: ['chaos'], queryFn: () => Promise.resolve(response) }),
  useUpdateChaos: () => ({ mutate, isPending: false }),
}));

describe('ChaosPanel (frontend-architecture §9.5)', () => {
  it('renders one card per canonical chaos mode (7 modes)', async () => {
    renderWithProviders(<ChaosPanel workspaceId="ws-1" streamId="s-1" />);

    for (const label of [
      'Duplicates',
      'Late arriving',
      'Missing',
      'Out of order',
      'Corrupted values',
      'Nulls',
      'Schema drift',
    ]) {
      expect(await screen.findByRole('heading', { name: label })).toBeInTheDocument();
    }
    // The 7 mode enable switches.
    expect(screen.getAllByRole('switch')).toHaveLength(7);
  });

  it('applies a preset bundle to the form and PATCHes the expanded document', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChaosPanel workspaceId="ws-1" streamId="s-1" />);

    // Pick "Dedup 101" then confirm the diff dialog.
    await user.click(await screen.findByRole('button', { name: 'Dedup 101' }));
    expect(await screen.findByText(/Apply .Dedup 101/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Apply preset' }));

    await waitFor(() => expect(mutate).toHaveBeenCalledTimes(1));
    const doc = mutate.mock.calls[0][0];
    expect(doc.duplicates.enabled).toBe(true);
    expect(doc.duplicates.rate).toBeCloseTo(0.05);
    // The preset REPLACES the whole document — unlisted modes are disabled.
    expect(doc.late_arriving.enabled).toBe(false);
    expect(doc.on_stop_policy).toBe('discard');
  });

  it('disables schema_drift with the INV-REG-5 note when no next version exists', async () => {
    renderWithProviders(
      <ChaosPanel workspaceId="ws-1" streamId="s-1" hasNextSchemaVersion={false} />,
    );
    await screen.findByRole('heading', { name: 'Schema drift' });
    expect(screen.getByText(/cannot arm until a next schema version/i)).toBeInTheDocument();
    const driftSwitch = screen.getByRole('switch', { name: /Enable Schema drift/i });
    expect(driftSwitch).toBeDisabled();
  });
});
