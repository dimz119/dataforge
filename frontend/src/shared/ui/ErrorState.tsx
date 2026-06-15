import { ApiError } from '../api/problem';
import { Button } from './Button';
import { cn } from '../lib/cn';

export interface ErrorStateProps {
  /** The thrown error; an ApiError surfaces its title/detail/request id (§10.1). */
  error: unknown;
  /** Retry callback (e.g. `query.refetch`); omit to hide the button. */
  onRetry?: () => void;
  className?: string;
}

function describe(error: unknown): { title: string; detail?: string; requestId?: string } {
  if (error instanceof ApiError) {
    return { title: error.title, detail: error.detail, requestId: error.requestId };
  }
  if (error instanceof Error) return { title: 'Something went wrong', detail: error.message };
  return { title: 'Something went wrong' };
}

/**
 * Generic error surface (frontend-architecture §10.1, last row). Renders the
 * problem title + detail and the `request_id` as the support handle. Used for
 * unmapped/unparseable problems; specific problem types get specialized UI
 * (form pointers, OverlayErrorMap, toasts) closer to the action.
 */
export function ErrorState({ error, onRetry, className }: ErrorStateProps) {
  const { title, detail, requestId } = describe(error);
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center justify-center rounded-lg border border-border bg-surface px-6 py-10 text-center',
        className,
      )}
    >
      <h2 className="text-base font-semibold text-text">{title}</h2>
      {detail && <p className="mt-1 max-w-md text-sm text-text-muted">{detail}</p>}
      {requestId && (
        <p className="mt-2 font-mono text-xs text-text-muted">request id: {requestId}</p>
      )}
      {onRetry && (
        <Button variant="secondary" size="sm" className="mt-4" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
