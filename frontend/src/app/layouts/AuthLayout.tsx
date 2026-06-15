import { Suspense } from 'react';
import { Outlet } from 'react-router';

import { PageSkeleton } from '../../shared/ui';

/**
 * Centered-card layout for the unauthenticated auth pages (frontend-architecture
 * §8). Hosts login/signup/verify/reset inside a single surface card.
 */
export function AuthLayout() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-6">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <span className="text-xl font-bold tracking-tight text-text">DataForge</span>
        </div>
        <div className="rounded-lg border border-border bg-surface p-6 shadow-sm">
          <Suspense fallback={<PageSkeleton />}>
            <Outlet />
          </Suspense>
        </div>
      </div>
    </main>
  );
}
