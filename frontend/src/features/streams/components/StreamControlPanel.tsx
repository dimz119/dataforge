import { StatusBadge } from '../../../shared/ui';
import type { StreamResponse } from '../../../shared/api/types';
import { controlRow } from '../controlMatrix';
import { LifecycleButtons } from './LifecycleButtons';
import { PinSummary } from './PinSummary';
import { StreamDangerZone } from './StreamDangerZone';
import { TpsSlider } from './TpsSlider';

export interface StreamControlPanelProps {
  workspaceId: string;
  stream: StreamResponse;
  /** Per-stream plan cap for the TPS slider (PRD §7). Defaults to the contract max. */
  tpsCap?: number;
}

/**
 * The `control` tab of the stream detail page (frontend-architecture §9.5). Composes
 * LifecycleButtons (matrix-driven), the log-scale TpsSlider (only while running, per
 * the matrix `tps` cell), the read-only PinSummary, and the StreamDangerZone. The
 * `chaos` and `answer-key` tabs are Phase 9 — the tab bar (rendered by the page) is
 * control-only now.
 */
export function StreamControlPanel({ workspaceId, stream, tpsCap }: StreamControlPanelProps) {
  const row = controlRow(stream.status);

  return (
    <div className="space-y-6">
      <section
        aria-labelledby="controls-heading"
        className="space-y-4 rounded-lg border border-border bg-surface p-5"
      >
        <div className="flex items-center justify-between">
          <h2 id="controls-heading" className="text-sm font-semibold text-text">
            Controls
          </h2>
          <StatusBadge status={stream.status} />
        </div>
        <LifecycleButtons
          workspaceId={workspaceId}
          streamId={stream.stream_id}
          status={stream.status}
        />
        {row.tps === 'enabled' && (
          <div className="border-t border-border pt-4">
            <TpsSlider
              workspaceId={workspaceId}
              streamId={stream.stream_id}
              value={stream.desired_state.target_tps}
              cap={tpsCap}
            />
          </div>
        )}
      </section>

      <PinSummary stream={stream} />
      <StreamDangerZone status={stream.status} />
    </div>
  );
}
