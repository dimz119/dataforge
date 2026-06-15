import { Suspense } from 'react';
import { Outlet, useParams } from 'react-router';

import { useSession } from '../../features/auth';
import { ErrorBoundary, PageSkeleton } from '../../shared/ui';
import { SideNav } from './SideNav';
import { UserMenu } from './UserMenu';
import { VerifyEmailBanner } from './VerifyEmailBanner';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';

/**
 * Workspace shell mounted at /w/:slug (frontend-architecture §3.1): TopBar
 * (brand + WorkspaceSwitcher + UserMenu), the VerifyEmailBanner for unverified
 * users, SideNav, and the route Outlet. Each lazy route chunk renders a
 * PageSkeleton as its Suspense fallback (§10.2); render errors are caught per
 * content area.
 */
export function WorkspaceLayout() {
  const { user } = useSession();
  const { slug = '' } = useParams();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between gap-4 border-b border-border bg-surface px-5 py-2.5">
        <div className="flex items-center gap-4">
          <span className="text-base font-bold tracking-tight text-text">DataForge</span>
          {user && <WorkspaceSwitcher memberships={user.memberships} activeSlug={slug} />}
        </div>
        {user && <UserMenu email={user.email} />}
      </header>

      {user && !user.is_verified && <VerifyEmailBanner email={user.email} />}

      <div className="flex flex-1">
        <SideNav slug={slug} />
        <main className="min-w-0 flex-1 p-6">
          <ErrorBoundary>
            <Suspense fallback={<PageSkeleton />}>
              <Outlet />
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
