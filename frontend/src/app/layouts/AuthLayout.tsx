import { Outlet } from 'react-router';

/**
 * Layout for the unauthenticated auth pages. Phase 7 replaces this with the
 * styled centered-card layout of the Console MVP.
 */
export function AuthLayout() {
  return (
    <main className="df-auth-layout">
      <Outlet />
    </main>
  );
}
