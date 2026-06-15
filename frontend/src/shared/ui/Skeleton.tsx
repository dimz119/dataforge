import { cn } from '../lib/cn';

export interface SkeletonProps {
  className?: string;
  /** Number of stacked lines to render (text-block skeleton). */
  lines?: number;
}

/**
 * Loading placeholder (frontend-architecture §10.2): skeleton screens, never
 * full-page spinners. Each lazy route renders a Skeleton as its Suspense
 * fallback; queries swap `isPending` widgets for these.
 */
export function Skeleton({ className, lines }: SkeletonProps) {
  if (lines && lines > 0) {
    return (
      <div className="flex flex-col gap-2" aria-hidden="true">
        {Array.from({ length: lines }).map((_, i) => (
          <span
            key={i}
            className={cn('h-4 w-full animate-pulse rounded bg-surface-muted', className)}
          />
        ))}
      </div>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={cn('block h-4 w-full animate-pulse rounded bg-surface-muted', className)}
    />
  );
}

/** Full-page route skeleton: a header band + a content block (§10.2). */
export function PageSkeleton() {
  return (
    <div className="p-6" role="status" aria-label="Loading">
      <Skeleton className="mb-6 h-7 w-48" />
      <Skeleton lines={5} className="h-12" />
    </div>
  );
}
