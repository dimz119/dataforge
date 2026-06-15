import * as Toast from '@radix-ui/react-toast';
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

import { ApiError } from '../api/problem';
import { cn } from '../lib/cn';

export type ToastTone = 'info' | 'success' | 'error';

export interface ToastInput {
  title: string;
  description?: string;
  tone?: ToastTone;
  /** Auto-dismiss after N ms; 0 keeps it until dismissed. */
  durationMs?: number;
}

interface ActiveToast extends ToastInput {
  id: number;
}

interface ToastApi {
  show: (input: ToastInput) => void;
  /** Convenience: surface an ApiError's title/detail as an error toast (§10.1). */
  showError: (error: unknown, fallbackTitle?: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

let nextId = 1;

/**
 * Toast provider (frontend-architecture §8, §10.1). `conflict`/`rate-limited`
 * problems surface here (§10.1); the `showError` helper maps an ApiError to a
 * toast. Wrapped at the composition root by AppProviders.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ActiveToast[]>([]);

  const remove = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const show = useCallback((input: ToastInput) => {
    setToasts((t) => [...t, { ...input, id: nextId++ }]);
  }, []);

  const showError = useCallback(
    (error: unknown, fallbackTitle = 'Something went wrong') => {
      if (error instanceof ApiError) {
        show({ title: error.title, description: error.detail, tone: 'error' });
      } else {
        show({ title: fallbackTitle, tone: 'error' });
      }
    },
    [show],
  );

  const api = useMemo<ToastApi>(() => ({ show, showError }), [show, showError]);

  return (
    <ToastContext.Provider value={api}>
      <Toast.Provider swipeDirection="right">
        {children}
        {toasts.map((t) => (
          <Toast.Root
            key={t.id}
            duration={t.durationMs ?? 5000}
            onOpenChange={(open) => !open && remove(t.id)}
            className={cn(
              'rounded-md border bg-surface px-4 py-3 shadow-md',
              t.tone === 'error' && 'border-danger',
              t.tone === 'success' && 'border-success',
              (!t.tone || t.tone === 'info') && 'border-border',
            )}
          >
            <Toast.Title className="text-sm font-semibold text-text">{t.title}</Toast.Title>
            {t.description && (
              <Toast.Description className="mt-0.5 text-xs text-text-muted">
                {t.description}
              </Toast.Description>
            )}
          </Toast.Root>
        ))}
        <Toast.Viewport className="fixed bottom-0 right-0 z-50 flex w-96 max-w-[100vw] flex-col gap-2 p-4 outline-none" />
      </Toast.Provider>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used within <ToastProvider>.');
  return ctx;
}
