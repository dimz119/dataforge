import { Button, useToast } from '../../../shared/ui';
import { ApiError } from '../../../shared/api/problem';
import {
  ACTION_FOR,
  controlRow,
  QUOTA_RESUME_TOOLTIP,
  startHint,
  startLabel,
  type ControlState,
} from '../controlMatrix';
import { useStreamLifecycle } from '../api';

export interface LifecycleButtonsProps {
  workspaceId: string;
  streamId: string;
  status: string;
}

/** Map a control state to whether the button renders, and how. */
function visible(state: ControlState): boolean {
  return state !== 'hidden';
}

/**
 * Start / Pause / Resume / Stop, driven entirely by the normative §9.5 matrix
 * (controlMatrix.ts). A `pending` cell shows the spinner+disabled state while the
 * desired-state POST settles; the sole `disabled` cell is `paused_quota` resume
 * (T7 guard) which carries the headroom tooltip. Verbs are idempotent (INV-STR-3),
 * so a stray double-click is harmless.
 */
export function LifecycleButtons({ workspaceId, streamId, status }: LifecycleButtonsProps) {
  const row = controlRow(status);
  const toast = useToast();
  const lifecycle = useStreamLifecycle(workspaceId, streamId);

  function run(verb: 'start' | 'pause' | 'resume' | 'stop') {
    lifecycle.mutate(ACTION_FOR[verb], {
      onError: (err) => {
        if (err instanceof ApiError && err.slug === 'quota-exceeded') {
          toast.show({
            title: 'Quota exceeded',
            description: err.detail,
            tone: 'error',
          });
          return;
        }
        toast.showError(err, 'Command failed');
      },
    });
  }

  // Only one verb is ever in flight; reflect pending on the matrix's pending cell.
  const pendingVerb = lifecycle.isPending ? lifecycle.variables : undefined;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {visible(row.start) && (
        <div className="flex flex-col items-start">
          <Button
            onClick={() => run('start')}
            disabled={row.start !== 'enabled'}
            loading={row.start === 'pending' || pendingVerb === 'start'}
          >
            {startLabel(status)}
          </Button>
          {startHint(status) && (
            <span className="mt-1 text-[11px] text-text-muted">{startHint(status)}</span>
          )}
        </div>
      )}

      {visible(row.pause) && (
        <Button
          variant="secondary"
          onClick={() => run('pause')}
          disabled={row.pause !== 'enabled'}
          loading={row.pause === 'pending' || pendingVerb === 'pause'}
        >
          Pause
        </Button>
      )}

      {visible(row.resume) && (
        <Button
          variant="secondary"
          onClick={() => run('resume')}
          disabled={row.resume !== 'enabled'}
          loading={row.resume === 'pending' || pendingVerb === 'resume'}
          title={row.resume === 'disabled' ? QUOTA_RESUME_TOOLTIP : undefined}
          aria-describedby={row.resume === 'disabled' ? `${streamId}-resume-hint` : undefined}
        >
          Resume
        </Button>
      )}

      {row.resume === 'disabled' && (
        <span id={`${streamId}-resume-hint`} className="text-[11px] text-status-amber">
          {QUOTA_RESUME_TOOLTIP}
        </span>
      )}

      {visible(row.stop) && (
        <Button
          variant="danger"
          onClick={() => run('stop')}
          disabled={row.stop !== 'enabled'}
          loading={row.stop === 'pending' || pendingVerb === 'stop'}
        >
          Stop
        </Button>
      )}
    </div>
  );
}
