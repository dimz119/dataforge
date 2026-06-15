import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { useNavigate } from 'react-router';

import type { MembershipSummary } from '../../shared/api/types';
import { cn } from '../../shared/lib/cn';

export interface WorkspaceSwitcherProps {
  memberships: readonly MembershipSummary[];
  /** The currently active workspace slug from the route. */
  activeSlug: string;
}

/**
 * Workspace switcher (frontend-architecture §3.1, §9.3). Lists the user's
 * memberships from the session; selecting one navigates to its dashboard. A
 * "Create workspace" entry routes to /workspaces/new (verified-gating is enforced
 * by that page + the API). Built on Radix DropdownMenu for ARIA/keyboard support.
 */
export function WorkspaceSwitcher({ memberships, activeSlug }: WorkspaceSwitcherProps) {
  const navigate = useNavigate();
  const active = memberships.find((m) => m.slug === activeSlug);

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        className={cn(
          'inline-flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-1.5',
          'text-sm font-medium text-text hover:bg-surface-muted focus:outline-none',
        )}
      >
        <span className="max-w-[12rem] truncate">{active?.name ?? activeSlug}</span>
        <span aria-hidden="true" className="text-text-muted">
          ▾
        </span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          className="z-40 min-w-56 rounded-md border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Label className="px-2 py-1 text-xs uppercase tracking-wide text-text-muted">
            Workspaces
          </DropdownMenu.Label>
          {memberships.map((m) => (
            <DropdownMenu.Item
              key={m.workspace_id}
              onSelect={() => {
                void navigate(`/w/${m.slug}/dashboard`);
              }}
              className={cn(
                'flex cursor-pointer items-center justify-between rounded px-2 py-1.5 text-sm outline-none',
                'data-[highlighted]:bg-surface-muted',
                m.slug === activeSlug ? 'font-semibold text-accent' : 'text-text',
              )}
            >
              <span className="truncate">{m.name}</span>
              <span className="ml-2 shrink-0 text-xs text-text-muted">{m.role}</span>
            </DropdownMenu.Item>
          ))}
          <DropdownMenu.Separator className="my-1 h-px bg-border" />
          <DropdownMenu.Item
            onSelect={() => {
              void navigate('/workspaces/new');
            }}
            className="cursor-pointer rounded px-2 py-1.5 text-sm text-accent outline-none data-[highlighted]:bg-surface-muted"
          >
            + Create workspace
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
