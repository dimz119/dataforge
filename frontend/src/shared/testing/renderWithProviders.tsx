import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';

/**
 * Test renderer with a fresh QueryClient per call (frontend-architecture §11.1).
 * Routing is the caller's concern: pass a `<RouterProvider>` over a memory router
 * (or wrap in `MemoryRouter`) as `ui`. Phase 7 expands this helper with
 * ToastProvider/ConfirmProvider and typed MSW handlers.
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
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}
