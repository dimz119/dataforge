import { useEffect, useRef, type ReactNode } from 'react';

import { cn } from '../lib/cn';

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  /** Right-aligned actions (e.g. a primary "Create" button). */
  actions?: ReactNode;
  className?: string;
}

/**
 * Page heading band (frontend-architecture §8). On mount it moves focus to the
 * heading so route changes land keyboard/SR focus at the top of the new page
 * (§8 accessibility: "focus moves to PageHeader on route change").
 */
export function PageHeader({ title, description, actions, className }: PageHeaderProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <header className={cn('mb-6 flex items-start justify-between gap-4', className)}>
      <div className="min-w-0">
        <h1
          ref={headingRef}
          tabIndex={-1}
          className="text-xl font-semibold tracking-tight text-text outline-none"
        >
          {title}
        </h1>
        {description && <p className="mt-1 text-sm text-text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
