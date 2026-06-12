import { screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { renderWithProviders } from '../shared/testing/renderWithProviders';
import { routes } from './router';

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return renderWithProviders(<RouterProvider router={router} />);
}

describe('app routing skeleton (Phase 1 smoke test)', () => {
  it('renders the workspace-resolver placeholder at /', async () => {
    renderAt('/');
    expect(await screen.findByRole('heading', { name: 'Workspace resolver' })).toBeInTheDocument();
  });

  it('resolves a workspace-scoped route inside the workspace layout', async () => {
    renderAt('/w/acme/dashboard');
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
    expect(screen.getByText('DataForge')).toBeInTheDocument();
  });

  it('resolves an auth route behind the PublicOnly stub', async () => {
    renderAt('/login');
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument();
  });

  it('renders NotFoundPage for unknown paths', async () => {
    renderAt('/definitely/not/a/route');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });
});
