export interface PlaceholderPageProps {
  /** Page-group name from the routing map (frontend-architecture §3.1). */
  group: string;
  /** Human-readable page name. */
  page: string;
  /** The phase in which the working page ships. */
  phase: number;
}

/**
 * Phase 1 routing-skeleton placeholder rendered by every routed page component
 * until its page group ships. Phase 7 (Console MVP) replaces the placeholder
 * usages with working pages; this component itself stays for later-phase routes.
 */
export function PlaceholderPage({ group, page, phase }: PlaceholderPageProps) {
  return (
    <section className="df-placeholder-page">
      <p className="df-placeholder-page__group">{group}</p>
      <h1>{page}</h1>
      <p>
        {group} — Phase {phase} (Console MVP). This route is part of the Phase 1 routing skeleton;
        the working page ships in Phase {phase}.
      </p>
    </section>
  );
}
