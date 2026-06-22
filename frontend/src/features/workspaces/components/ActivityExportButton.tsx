import * as DropdownMenu from '@radix-ui/react-dropdown-menu';

import { cn } from '../../../shared/lib/cn';
import type { AuditEntry } from '../../../shared/api/types';
import {
  triggerActivityDownload,
  type ActivityExportFormat,
} from '../activityExport';

export interface ActivityExportButtonProps {
  /** The audit entries to export (the same list rendered by ActivityList). */
  entries: AuditEntry[];
  /** Workspace slug, used to stamp the download filename. */
  slug: string;
  disabled?: boolean;
}

/**
 * Activity-log export control (frontend-architecture §9.3 / Phase 11). A dropdown
 * offering CSV or JSON download of the workspace's audit entries, serialized client-side
 * from the already-fetched list (the audit endpoint has no server-side export). Disabled
 * when there are no entries to export.
 */
export function ActivityExportButton({ entries, slug, disabled }: ActivityExportButtonProps) {
  const isDisabled = disabled || entries.length === 0;

  function exportAs(format: ActivityExportFormat) {
    triggerActivityDownload(entries, slug, format);
  }

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        disabled={isDisabled}
        data-testid="activity-export-trigger"
        className={cn(
          'inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-sm font-medium text-text',
          'hover:bg-surface-muted focus:outline-none disabled:cursor-not-allowed disabled:opacity-50',
        )}
      >
        Export
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-40 min-w-36 rounded-md border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Item
            onSelect={() => exportAs('csv')}
            className="cursor-pointer rounded px-2 py-1.5 text-sm text-text outline-none data-[highlighted]:bg-surface-muted"
          >
            Download CSV
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={() => exportAs('json')}
            className="cursor-pointer rounded px-2 py-1.5 text-sm text-text outline-none data-[highlighted]:bg-surface-muted"
          >
            Download JSON
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
