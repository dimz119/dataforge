import type { ReactNode } from 'react';

import { cn } from '../lib/cn';

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  /** The single primary CTA — empty states are launch surfaces, not absences (§10.3). */
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
}

/**
 * Empty-collection placeholder (frontend-architecture §10.3). Every list page
 * supplies one with a single primary CTA.
 */
export function EmptyState({ title, description, action, icon, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-surface px-6 py-12 text-center',
        className,
      )}
    >
      {icon && <div className="mb-3 text-text-muted">{icon}</div>}
      <h2 className="text-base font-semibold text-text">{title}</h2>
      {description && <p className="mt-1 max-w-md text-sm text-text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
