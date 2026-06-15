import * as DropdownMenu from '@radix-ui/react-dropdown-menu';

import { cn } from '../../../shared/lib/cn';

export interface EventTypeFilterProps {
  types: string[];
  selected: string[];
  onChange: (selected: string[]) => void;
}

/**
 * Event-type multi-select filter for the live tail (frontend-architecture §9.7).
 * Changing the selection recreates the socket with a new server-side `types` filter
 * (WS-5; handled by `useStreamTail`). Built on Radix DropdownMenu for ARIA/keyboard.
 */
export function EventTypeFilter({ types, selected, onChange }: EventTypeFilterProps) {
  const toggle = (type: string, checked: boolean) => {
    onChange(checked ? [...selected, type] : selected.filter((t) => t !== type));
  };
  const label =
    selected.length === 0 ? 'All event types' : `${selected.length} type${selected.length > 1 ? 's' : ''}`;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs font-medium text-text hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-status-blue"
      >
        {label}
        <span aria-hidden>▾</span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={4}
          className="z-50 max-h-72 min-w-48 overflow-auto rounded-md border border-border bg-surface p-1 shadow-lg"
        >
          <DropdownMenu.Label className="px-2 py-1 text-xs uppercase tracking-wide text-text-muted">
            Filter event types
          </DropdownMenu.Label>
          {selected.length > 0 && (
            <button
              type="button"
              onClick={() => onChange([])}
              className="mb-1 w-full rounded px-2 py-1 text-left text-xs text-status-blue hover:bg-surface-muted"
            >
              Clear filter
            </button>
          )}
          {types.map((type) => {
            const checked = selected.includes(type);
            return (
              <DropdownMenu.CheckboxItem
                key={type}
                checked={checked}
                onCheckedChange={(c) => toggle(type, c === true)}
                onSelect={(e) => e.preventDefault()}
                className={cn(
                  'flex cursor-pointer items-center gap-2 rounded px-2 py-1 font-mono text-xs text-text outline-none data-[highlighted]:bg-surface-muted',
                )}
              >
                <span
                  aria-hidden
                  className={cn(
                    'flex h-3.5 w-3.5 items-center justify-center rounded border border-border text-[10px]',
                    checked && 'border-status-blue bg-status-blue text-white',
                  )}
                >
                  {checked ? '✓' : ''}
                </span>
                {type}
              </DropdownMenu.CheckboxItem>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
