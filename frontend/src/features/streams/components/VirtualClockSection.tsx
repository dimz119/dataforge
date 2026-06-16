import { FormField, Input } from '../../../shared/ui';
import { SPEED_MULTIPLIER_MAX, SPEED_MULTIPLIER_MIN, backfillDaysCap } from '../planCaps';

export type StreamMode = 'live' | 'backfill';

export interface VirtualClockValue {
  /** Virtual-clock speed multiplier (1–1000, api-spec §4.8). */
  speedMultiplier: number;
  /** `live` (the streaming runtime) or `backfill` (the datasets resource, §4.10). */
  mode: StreamMode;
  /** Simulated-day window for backfill, clamped to the plan cap. */
  backfillDays: number;
}

export interface VirtualClockSectionProps {
  value: VirtualClockValue;
  onChange: (next: VirtualClockValue) => void;
  /** Workspace plan string, for the backfill-day hard clamp (UX only; API enforces). */
  plan?: string;
}

/** Speed presets surfaced as quick buttons (frontend-architecture §9.5: 1×/60×/24×). */
const SPEED_PRESETS = [1, 60, 1440] as const;

function clampSpeed(n: number): number {
  if (Number.isNaN(n)) return SPEED_MULTIPLIER_MIN;
  return Math.min(SPEED_MULTIPLIER_MAX, Math.max(SPEED_MULTIPLIER_MIN, Math.round(n)));
}

/**
 * The virtual-clock controls on stream create (frontend-architecture §13, Phase 8).
 * Unlocks: a `speed_multiplier` slider (1–1000) with presets, and a `mode` selector
 * (`live` | `backfill`). `backfill` reveals a `backfill_days` input clamped to the
 * plan cap (Free 7 / Classroom 30 / Pro 90) — backfill is realized via the datasets
 * resource (api-spec §4.10), so the helper points there. Live emission honors
 * `speed_multiplier` across dwell times, latencies, and intensity curves (ADR-0008).
 */
export function VirtualClockSection({ value, onChange, plan }: VirtualClockSectionProps) {
  const daysCap = backfillDaysCap(plan);
  const set = (patch: Partial<VirtualClockValue>) => onChange({ ...value, ...patch });

  return (
    <fieldset className="rounded-md border border-border p-4">
      <legend className="px-1 text-sm font-medium text-text">Virtual clock</legend>

      <FormField
        label="Speed multiplier"
        hint="1×–1000× of wall time. Affects dwell times, lifecycle latencies, and intensity curves (ADR-0008)."
      >
        {(p) => (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-3">
              <input
                id={p.id}
                type="range"
                min={SPEED_MULTIPLIER_MIN}
                max={SPEED_MULTIPLIER_MAX}
                step={1}
                value={value.speedMultiplier}
                onChange={(e) => set({ speedMultiplier: clampSpeed(Number(e.target.value)) })}
                className="h-2 flex-1 cursor-pointer accent-status-blue"
                aria-label="Speed multiplier"
              />
              <span className="w-20 shrink-0 text-right font-mono tabular-nums text-sm text-text">
                {value.speedMultiplier}×
              </span>
            </div>
            <div className="flex gap-1.5">
              {SPEED_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  onClick={() => set({ speedMultiplier: preset })}
                  aria-pressed={value.speedMultiplier === preset}
                  className={
                    value.speedMultiplier === preset
                      ? 'rounded border border-status-blue bg-status-blue/15 px-2 py-0.5 text-xs font-medium text-status-blue'
                      : 'rounded border border-border px-2 py-0.5 text-xs text-text-muted hover:bg-surface-muted'
                  }
                >
                  {preset}×
                </button>
              ))}
            </div>
          </div>
        )}
      </FormField>

      <div className="mt-4 grid grid-cols-2 gap-4">
        <FormField label="Mode" hint="Backfill generates history as a dataset (§4.10).">
          {(p) => (
            <select
              id={p.id}
              value={value.mode}
              onChange={(e) => set({ mode: e.target.value as StreamMode })}
              className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text"
            >
              <option value="live">Live</option>
              <option value="backfill">Backfill</option>
            </select>
          )}
        </FormField>

        {value.mode === 'backfill' && (
          <FormField
            label="Backfill days"
            hint={`Simulated days of history (≤ ${daysCap} on this plan).`}
          >
            {(p) => (
              <Input
                type="number"
                min={1}
                max={daysCap}
                value={String(value.backfillDays)}
                onChange={(e) =>
                  set({
                    backfillDays: Math.min(
                      daysCap,
                      Math.max(1, Number.parseInt(e.target.value, 10) || 1),
                    ),
                  })
                }
                {...p}
              />
            )}
          </FormField>
        )}
      </div>

      {value.mode === 'backfill' && (
        <p className="mt-3 rounded bg-status-amber/10 px-3 py-2 text-xs text-status-amber">
          Backfill is realized as a downloadable dataset (Datasets resource). Create the
          stream, then start a backfill dataset job from its detail page.
        </p>
      )}
    </fieldset>
  );
}
