import { screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ApiKeyListItem } from '../../../shared/api/types';
import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import { KeysTable } from './KeysTable';

vi.mock('../api', () => ({
  useRevokeApiKey: () => ({ mutate: vi.fn(), isPending: false }),
}));

const item: ApiKeyListItem = {
  api_key_id: 'key-1',
  name: 'CI reader',
  prefix: 'df_live_a3f8',
  last4: '9c2e',
  scopes: ['events:read', 'streams:read'],
  state: 'active',
  last_used_at: null,
  expires_at: null,
  created_by: 'user-1',
  created_at: '2026-06-01T00:00:00Z',
};

describe('KeysTable (§9.6)', () => {
  it('shows only the masked prefix……last4, never a full key', () => {
    renderWithProviders(
      <KeysTable workspaceId="ws-1" keys={[item]} isLoading={false} error={null} />,
    );
    expect(screen.getByText('df_live_a3f8……9c2e')).toBeInTheDocument();
    // A full secret would contain the prefix joined to additional characters with
    // no mask — assert the masked separator is the only rendering of the prefix.
    expect(screen.queryByText(/df_live_a3f8_[A-Za-z0-9]/)).not.toBeInTheDocument();
  });

  it('renders the scope chips and name', () => {
    renderWithProviders(
      <KeysTable workspaceId="ws-1" keys={[item]} isLoading={false} error={null} />,
    );
    expect(screen.getByText('CI reader')).toBeInTheDocument();
    expect(screen.getByText('events:read')).toBeInTheDocument();
    expect(screen.getByText('streams:read')).toBeInTheDocument();
  });

  it('offers revoke only for active keys', () => {
    const revoked: ApiKeyListItem = { ...item, api_key_id: 'key-2', state: 'revoked' };
    renderWithProviders(
      <KeysTable workspaceId="ws-1" keys={[revoked]} isLoading={false} error={null} />,
    );
    expect(screen.queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument();
  });
});
