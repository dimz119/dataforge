import { screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { Membership } from '../../../shared/api/types';
import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import { MembersTable } from './MembersTable';

// Mock the mutation hooks so the table renders without a live transport.
vi.mock('../api', () => ({
  useUpdateMemberRole: () => ({ mutate: vi.fn(), isPending: false }),
  useRemoveMember: () => ({ mutate: vi.fn(), isPending: false }),
}));

const admin: Membership = {
  user_id: 'u-admin',
  email: 'ada@example.net',
  role: 'admin',
  joined_at: '2026-01-01T00:00:00Z',
};
const member: Membership = {
  user_id: 'u-member',
  email: 'mike@example.net',
  role: 'member',
  joined_at: '2026-02-01T00:00:00Z',
};

function rowFor(email: string): HTMLElement {
  return screen.getByText(email).closest('tr') as HTMLElement;
}

describe('MembersTable (INV-TEN-3)', () => {
  it('disables demote and remove for the sole admin', () => {
    renderWithProviders(
      <MembersTable
        workspaceId="ws-1"
        members={[admin, member]}
        isLoading={false}
        error={null}
        currentUserId="u-admin"
      />,
    );
    const adminRow = within(rowFor('ada@example.net'));
    expect(adminRow.getByRole('button', { name: 'Demote' })).toBeDisabled();
    expect(adminRow.getByRole('button', { name: 'Remove' })).toBeDisabled();
  });

  it('enables demote/remove once a second admin exists', () => {
    const secondAdmin: Membership = { ...member, role: 'admin' };
    renderWithProviders(
      <MembersTable
        workspaceId="ws-1"
        members={[admin, secondAdmin]}
        isLoading={false}
        error={null}
        currentUserId="u-admin"
      />,
    );
    const adminRow = within(rowFor('ada@example.net'));
    expect(adminRow.getByRole('button', { name: 'Demote' })).toBeEnabled();
    expect(adminRow.getByRole('button', { name: 'Remove' })).toBeEnabled();
  });

  it('allows removing a regular member', () => {
    renderWithProviders(
      <MembersTable
        workspaceId="ws-1"
        members={[admin, member]}
        isLoading={false}
        error={null}
        currentUserId="u-admin"
      />,
    );
    const memberRow = within(rowFor('mike@example.net'));
    expect(memberRow.getByRole('button', { name: 'Remove' })).toBeEnabled();
  });
});
