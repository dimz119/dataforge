import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

import { ApiError } from '../shared/api/problem';

/**
 * QueryClient defaults per frontend-architecture §4.1. 4xx responses are never
 * retried; network errors (status 0) and 5xx retry twice with the built-in
 * exponential delay. Mutations never auto-retry: lifecycle commands are
 * idempotent (INV-STR-3), but an auto-retry would hide failures.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        retry: (count, err) =>
          count < 2 && err instanceof ApiError && (err.status >= 500 || err.status === 0),
        refetchOnWindowFocus: true,
      },
      mutations: { retry: 0 },
    },
  });
}

/**
 * Composition-root providers. Phase 7 replaces this with the full provider
 * stack: QueryClientProvider + ToastProvider + ConfirmProvider
 * (frontend-architecture §2.1).
 */
export function AppProviders({ children }: { children: ReactNode }) {
  const [queryClient] = useState(createQueryClient);
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
