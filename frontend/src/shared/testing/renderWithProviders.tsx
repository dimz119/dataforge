import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';

import { ToastProvider } from '../ui/toast';

/**
 * Test renderer with a fresh QueryClient per call + the ToastProvider (so
 * components using `useToast` render in isolation) — frontend-architecture §11.1.
 * Routing is the caller's concern: pass a `<RouterProvider>`/`<MemoryRouter>` as
 * `ui`. Retries are off so error paths are deterministic.
 */
export function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: 0 },
    },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <ToastProvider>{ui}</ToastProvider>
      </QueryClientProvider>,
    ),
  };
}
