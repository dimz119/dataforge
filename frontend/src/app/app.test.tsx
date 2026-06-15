import { screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useRoutes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as authApi from '../features/auth/api';
import { renderWithProviders } from '../shared/testing/renderWithProviders';
import type { UserMeResponse } from '../shared/api/types';
import { routes } from './router';

const VERIFIED_USER: UserMeResponse = {
  user_id: 'u1',
  email: 'rosa@example.net',
  is_verified: true,
  created_at: '2026-06-10T09:12:44.118201Z',
  memberships: [{ workspace_id: 'ws1', name: 'Acme Cohort', slug: 'acme', role: 'admin' }],
};

function mockSession(user: UserMeResponse | null) {
  vi.spyOn(authApi, 'bootstrapSession').mockResolvedValue(user);
}

// Declarative router (not the data router) so guard redirects don't construct
// undici Requests in jsdom — we only exercise component-level guard logic here.
function AppRoutes() {
  return useRoutes(routes);
}

function renderAt(path: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('app routing + guards (Phase 7)', () => {
  it('redirects an authenticated user from / to their workspace dashboard', async () => {
    mockSession(VERIFIED_USER);
    renderAt('/');
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
    expect(screen.getByText('DataForge')).toBeInTheDocument();
  });

  it('renders the login page for an unauthenticated visitor under PublicOnly', async () => {
    mockSession(null);
    renderAt('/login');
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument();
  });

  it('redirects an unauthenticated visitor away from a guarded route to /login', async () => {
    mockSession(null);
    renderAt('/w/acme/dashboard');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Log in' })).toBeInTheDocument(),
    );
  });

  it('renders NotFoundPage when :slug is not a membership (cross-tenant masking)', async () => {
    mockSession(VERIFIED_USER);
    renderAt('/w/not-mine/dashboard');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });

  it('renders NotFoundPage for unknown paths', async () => {
    mockSession(null);
    renderAt('/definitely/not/a/route');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });
});
