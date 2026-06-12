import { Outlet } from 'react-router';

/**
 * Workspace shell mounted at /w/:slug. Phase 7 replaces this with the real
 * TopBar (workspace switcher, user menu) + SideNav + VerifyEmailBanner layout
 * (frontend-architecture §3.1).
 */
export function WorkspaceLayout() {
  return (
    <div className="df-workspace-layout">
      <header className="df-workspace-layout__topbar">
        <span className="df-workspace-layout__brand">DataForge</span>
        <span className="df-workspace-layout__hint">Workspace shell — Phase 7 (Console MVP)</span>
      </header>
      <main className="df-workspace-layout__content">
        <Outlet />
      </main>
    </div>
  );
}
