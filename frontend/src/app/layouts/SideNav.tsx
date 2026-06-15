import { NavLink } from 'react-router';

import { cn } from '../../shared/lib/cn';

interface NavItem {
  to: string;
  label: string;
}

/**
 * Workspace SideNav (frontend-architecture §3.1). Renders the Phase-7 navigation
 * for the active workspace. Reserved-not-rendered slots (per the deferral rules):
 *  - "Schemas" (registry browser) → Phase 10
 *  - "Channels" (external sinks) → Phase 12
 * Their feature folders/routes do not exist yet; the slots are commented so the
 * nav order is stable when they land.
 */
export function SideNav({ slug }: { slug: string }) {
  const base = `/w/${slug}`;
  const items: NavItem[] = [
    { to: `${base}/dashboard`, label: 'Dashboard' },
    { to: `${base}/scenarios`, label: 'Scenarios' },
    { to: `${base}/streams`, label: 'Streams' },
    { to: `${base}/monitoring`, label: 'Monitoring' },
    { to: `${base}/api-keys`, label: 'API keys' },
    { to: `${base}/settings`, label: 'Settings' },
    // Phase 10: { to: `${base}/schemas`, label: 'Schemas' },
    // Phase 12: { to: `${base}/channels`, label: 'Channels' },
  ];

  return (
    <nav aria-label="Workspace" className="w-52 shrink-0 border-r border-border bg-surface p-3">
      <ul className="flex flex-col gap-1">
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'block rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-accent/10 text-accent'
                    : 'text-text-muted hover:bg-surface-muted hover:text-text',
                )
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
