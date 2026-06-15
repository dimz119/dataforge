import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

import { resolveProblem } from '../shared/api/handleProblem';
import { ApiError } from '../shared/api/problem';
import { ErrorBoundary } from '../shared/ui/ErrorBoundary';
import { ToastProvider, useToast } from '../shared/ui/toast';

/** A toast surface the MutationCache safety net can call (decoupled from the UI pkg). */
export interface MutationErrorSink {
  show: (input: { title: string; description?: string; tone?: 'info' | 'success' | 'error' }) => void;
}

/**
 * QueryClient defaults per frontend-architecture §4.1. 4xx responses are never
 * retried; network errors (status 0) and 5xx retry twice with the built-in
 * exponential delay. Mutations never auto-retry: lifecycle commands are
 * idempotent (INV-STR-3), but an auto-retry would hide failures. Per-query
 * `staleTime` overrides live in `shared/api/queryKeys.ts` (§4.2) and are applied
 * by each feature's `queryOptions`.
 *
 * The MutationCache `onError` is the §10.1 safety net: any mutation error a
 * component does NOT handle locally (the component passes its own `onError`,
 * which suppresses this — TanStack only fires the cache handler when the local
 * one is absent OR re-throws) surfaces as a toast classified by the central
 * problem switch. Form/overlay/page-not-found errors are always handled locally,
 * so this default only ever fires for the generic/conflict/quota tail.
 */
export function createQueryClient(toast?: MutationErrorSink): QueryClient {
  return new QueryClient({
    mutationCache: new MutationCache({
      onError: (err, _vars, _ctx, mutation) => {
        // Respect a component-supplied onError: do not double-report.
        if (mutation.options.onError || !toast) return;
        const action = resolveProblem(err);
        switch (action.kind) {
          case 'toast':
            toast.show({ title: action.title, description: action.detail, tone: 'error' });
            break;
          case 'quota':
            toast.show({
              title: 'Quota exceeded',
              description: action.detail,
              tone: 'error',
            });
            break;
          case 'rate-limited':
            toast.show({
              title: 'Rate limited',
              description:
                action.detail ?? `Try again in ${String(action.retryAfter)} seconds.`,
              tone: 'error',
            });
            break;
          case 'generic':
            toast.show({
              title: action.error.title,
              description: action.error.detail,
              tone: 'error',
            });
            break;
          // form / overlay / page-not-found / cursor-expired / auth are surfaced
          // by the owning component or middleware, never by this safety net.
          default:
            break;
        }
      },
    }),
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

/** Wires the QueryClient's mutation safety net to the live toast surface. */
function QueryRoot({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [queryClient] = useState(() => createQueryClient(toast));
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

/**
 * Composition-root providers (frontend-architecture §2.1): the render error
 * boundary, ToastProvider, and QueryClientProvider. ToastProvider wraps the
 * QueryClient so the MutationCache safety net (§10.1) can raise toasts. The
 * session bootstrap (§6.2) is driven by RequireAuth, not a provider, so it
 * suspends per-route.
 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ErrorBoundary>
      <ToastProvider>
        <QueryRoot>{children}</QueryRoot>
      </ToastProvider>
    </ErrorBoundary>
  );
}
