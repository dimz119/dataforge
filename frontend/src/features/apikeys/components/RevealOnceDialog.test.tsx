import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type { ApiKeyCreated } from '../../../shared/api/types';
import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import { RevealOnceDialog } from './RevealOnceDialog';

const PLAINTEXT = 'df_live_a3f8_supersecretvalue9c2e';

function makeCreated(): ApiKeyCreated {
  return {
    api_key_id: 'key-1',
    workspace_id: 'ws-1',
    name: 'CI reader',
    key: PLAINTEXT,
    prefix: 'df_live_a3f8',
    last4: '9c2e',
    scopes: ['events:read'],
    state: 'active',
    expires_at: null,
    created_by: 'user-1',
    created_at: '2026-06-14T00:00:00Z',
  };
}

/** A harness that mirrors the page: `created` lives in parent state, nulled onClose. */
function Harness() {
  const [created, setCreated] = useState<ApiKeyCreated | null>(makeCreated());
  return <RevealOnceDialog created={created} onClose={() => setCreated(null)} />;
}

describe('RevealOnceDialog (INV-TEN-4)', () => {
  it('shows the plaintext key exactly once while open', () => {
    renderWithProviders(<Harness />);
    // The plaintext appears in the copy field while the dialog is open.
    expect(screen.getByText(PLAINTEXT)).toBeInTheDocument();
  });

  it('clears the plaintext from the DOM after closing (copied path)', async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);
    expect(screen.getByText(PLAINTEXT)).toBeInTheDocument();

    // Mark copied so "Done" closes without the confirm step.
    await user.click(screen.getByRole('button', { name: 'I have copied this key' }));
    await user.click(screen.getByRole('button', { name: 'Done' }));

    await waitFor(() => {
      expect(screen.queryByText(PLAINTEXT)).not.toBeInTheDocument();
    });
  });

  it('requires a confirm to close without copying, then clears the plaintext', async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    // Closing without copying surfaces the confirm dialog — key still not gone yet.
    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(await screen.findByText('Close without copying?')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Close anyway' }));
    await waitFor(() => {
      expect(screen.queryByText(PLAINTEXT)).not.toBeInTheDocument();
    });
  });

  it('renders nothing in the DOM when created is null (never persisted)', () => {
    const onClose = vi.fn();
    renderWithProviders(<RevealOnceDialog created={null} onClose={onClose} />);
    expect(screen.queryByText(PLAINTEXT)).not.toBeInTheDocument();
    expect(screen.queryByText('API key created')).not.toBeInTheDocument();
  });
});
