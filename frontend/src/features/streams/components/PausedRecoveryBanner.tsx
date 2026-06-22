import { Button, useToast } from '../../../shared/ui';
import { ApiError } from '../../../shared/api/problem';
import type { StreamResponse } from '../../../shared/api/types';
import { QUOTA_RESUME_TOOLTIP } from '../controlMatrix';
import { useStreamLifecycle } from '../api';

export interface PausedRecoveryBannerProps {
  workspaceId: string;
  stream: StreamResponse;
}

/**
 * Recovery banner for a system-paused stream (Phase 11, frontend-architecture §9.5).
 * Renders ONLY for the two system-pause states; a user `paused` stream uses the plain
 * control buttons. The banner explains WHY the stream paused (the `status_reason` the
 * runner wrote when it called `system_pause`) and offers the appropriate recovery:
 *
 *  - `paused_idle`  → a direct one-click "Resume" (the idle pause clears the moment the
 *    stream runs again; the matrix already enables resume in this state).
 *  - `paused_quota` → resume is GUARDED (T7): it stays unavailable until consumption
 *    drops under cap, so we explain the guard rather than offering a button that the
 *    server would reject. This mirrors the disabled `resume` matrix cell.
 *
 * Resume is idempotent (INV-STR-3); the lifecycle hook optimistically flips the status
 * to `resuming` so the badge converges via the §4.4 poll.
 */
const SYSTEM_PAUSE_STATES = new Set(['paused_quota', 'paused_idle']);

export function PausedRecoveryBanner({ workspaceId, stream }: PausedRecoveryBannerProps) {
  const toast = useToast();
  const lifecycle = useStreamLifecycle(workspaceId, stream.stream_id);

  if (!SYSTEM_PAUSE_STATES.has(stream.status)) return null;

  const isQuota = stream.status === 'paused_quota';
  const title = isQuota ? 'Paused — quota exhausted' : 'Paused — idle';
  const explanation = isQuota
    ? 'This stream hit its workspace quota and was paused automatically to protect your data. No events were lost; the buffered events and checkpoint are intact.'
    : 'This stream was paused automatically after a period without consumers. Its checkpoint is intact — resume to continue from where it left off.';

  function resume() {
    lifecycle.mutate('resume', {
      onError: (err) => {
        // A quota-still-exhausted resume is rejected server-side (T7 guard).
        if (err instanceof ApiError && err.slug === 'quota-exceeded') {
          toast.show({
            title: 'Resume unavailable',
            description: QUOTA_RESUME_TOOLTIP,
            tone: 'error',
          });
          return;
        }
        toast.showError(err, 'Resume failed');
      },
    });
  }

  return (
    <section
      role="status"
      aria-labelledby="recovery-heading"
      data-testid="paused-recovery-banner"
      data-reason={stream.status}
      className="rounded-lg border border-status-amber/40 bg-status-amber/10 p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id="recovery-heading" className="text-sm font-semibold text-text">
            {title}
          </h3>
          <p className="mt-1 text-sm text-text-muted">{explanation}</p>
          {stream.status_reason && (
            <p className="mt-1 text-xs text-text-muted">
              Reason: <span className="font-mono">{stream.status_reason}</span>
            </p>
          )}
        </div>
        {isQuota ? (
          <span
            className="shrink-0 rounded-md border border-status-amber/40 px-3 py-1.5 text-xs font-medium text-status-amber"
            data-testid="quota-resume-guard"
          >
            Resume available when consumption is under cap
          </span>
        ) : (
          <Button
            variant="secondary"
            onClick={resume}
            loading={lifecycle.isPending}
            data-testid="idle-resume-button"
          >
            Resume
          </Button>
        )}
      </div>
    </section>
  );
}
