import * as Slider from '@radix-ui/react-slider';
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../../../shared/api/problem';
import { useToast } from '../../../shared/ui';
import { useSetTargetTps } from '../api';
import { positionToTps, tpsToPosition, TPS_MAX } from '../tpsScale';

export interface TpsSliderProps {
  workspaceId: string;
  streamId: string;
  /** The current effective target_tps (from desired_state.target_tps). */
  value: number;
  /** Plan per-stream cap (PRD §7: Free 50 / Classroom 100 / Pro 1,000). */
  cap?: number;
  /** Debounce window before the optimistic PATCH fires (§9.5: 400 ms). */
  debounceMs?: number;
}

/**
 * Log-scale target_tps slider (frontend-architecture §9.5 TpsSlider). Drag is local
 * state on a [0, 1000] log position; on release the value is debounced 400 ms then
 * written optimistically via PATCH (§4.8.2, PIN-3). Hard-clamped to the plan cap.
 * Helper: "takes effect within 2 s". Quota rejections (403) revert and toast.
 */
export function TpsSlider({
  workspaceId,
  streamId,
  value,
  cap = TPS_MAX,
  debounceMs = 400,
}: TpsSliderProps) {
  const toast = useToast();
  const setTps = useSetTargetTps(workspaceId, streamId);
  const POS_STEPS = 1000; // slider granularity over the log position
  const positionFor = useCallback(
    (tps: number) => Math.round(tpsToPosition(tps, cap) * POS_STEPS),
    [cap],
  );

  // `local` is the dragged position; null means "follow the server value".
  const [local, setLocal] = useState<number | null>(null);
  const timer = useRef<number | undefined>(undefined);

  // Clear any pending debounce on unmount.
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const sliderPos = local ?? positionFor(value);
  const previewTps = positionToTps(sliderPos / POS_STEPS, cap);

  const commit = useCallback(
    (pos: number) => {
      const next = positionToTps(pos / POS_STEPS, cap);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        setTps.mutate(next, {
          onError: (err) => {
            setLocal(null); // revert to the server value
            if (err instanceof ApiError && err.slug === 'quota-exceeded') {
              toast.show({ title: 'Quota exceeded', description: err.detail, tone: 'error' });
            } else {
              toast.showError(err, 'Could not change rate');
            }
          },
          onSuccess: () => setLocal(null),
        });
      }, debounceMs);
    },
    [cap, debounceMs, setTps, toast],
  );

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between">
        <label htmlFor={`${streamId}-tps`} className="text-sm font-medium text-text">
          Target rate
        </label>
        <span className="font-mono text-sm text-text" aria-live="polite">
          {previewTps.toLocaleString('en-US')} TPS
        </span>
      </div>
      <Slider.Root
        id={`${streamId}-tps`}
        className="relative flex h-5 w-full touch-none select-none items-center"
        min={0}
        max={POS_STEPS}
        step={1}
        value={[sliderPos]}
        onValueChange={([p]) => setLocal(p ?? 0)}
        onValueCommit={([p]) => commit(p ?? 0)}
      >
        <Slider.Track className="relative h-1.5 w-full grow rounded-full bg-surface-muted">
          <Slider.Range className="absolute h-full rounded-full bg-accent" />
        </Slider.Track>
        <Slider.Thumb
          aria-label="Target events per second"
          aria-valuetext={`${previewTps.toLocaleString('en-US')} events per second`}
          className="block h-4 w-4 rounded-full border-2 border-accent bg-surface shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </Slider.Root>
      <div className="flex justify-between text-[11px] text-text-muted">
        <span>1</span>
        <span>Takes effect within 2&nbsp;s · plan cap {cap.toLocaleString('en-US')}</span>
        <span>{cap.toLocaleString('en-US')}</span>
      </div>
    </div>
  );
}
