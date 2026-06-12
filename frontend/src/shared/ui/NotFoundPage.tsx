/**
 * Catch-all `*` route (frontend-architecture §3.1). Also the rendering target of the
 * RequireWorkspace / RequireAdmin guard failures once they go live in Phase 7 —
 * cross-tenant probes must be indistinguishable from missing resources.
 */
export function NotFoundPage() {
  return (
    <section className="df-not-found-page">
      <h1>Page not found</h1>
      <p>The address does not exist, or you do not have access to it.</p>
    </section>
  );
}
