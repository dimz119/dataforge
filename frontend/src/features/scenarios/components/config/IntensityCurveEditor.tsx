import { Input } from '../../../../shared/ui';
import { INTENSITY_MAX, type DiurnalBucket, type WeeklyCurve } from '../../overlay';
import type { OverlayErrorMap } from '../../overlayErrors';

export interface IntensityCurveEditorProps {
  diurnal: DiurnalBucket[];
  weekly: WeeklyCurve;
  onDiurnalChange: (index: number, multiplier: number) => void;
  onWeeklyChange: (day: string, multiplier: number) => void;
  errors: OverlayErrorMap;
}

const DAY_ORDER = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

function clampMult(raw: number): number {
  if (Number.isNaN(raw)) return 0;
  return Math.max(0, Math.min(INTENSITY_MAX, raw));
}

/**
 * Intensity-curve editor (frontend-architecture §9.4 IntensityCurveEditor): the
 * 24-hour diurnal bars + 7-day weekly inputs (0–10, B-15). The engine renormalizes
 * the curve to mean 1.0, so curve edits never change average TPS (PRD §4.3) — stated
 * explicitly below. Errors keyed `intensity` surface at the top.
 */
export function IntensityCurveEditor({
  diurnal,
  weekly,
  onDiurnalChange,
  onWeeklyChange,
  errors,
}: IntensityCurveEditorProps) {
  const intensityErrors = errors.intensity ?? [];
  const peak = Math.max(1, ...diurnal.map((b) => b.multiplier));

  return (
    <div className="space-y-5">
      {intensityErrors.length > 0 && (
        <ul role="alert" className="space-y-1">
          {intensityErrors.map((e, i) => (
            <li key={i} className="text-xs text-danger">
              {e.message}
            </li>
          ))}
        </ul>
      )}

      <div>
        <h4 className="mb-2 text-sm font-medium text-text">Diurnal (24-hour)</h4>
        <div className="flex items-end gap-1" aria-hidden="true">
          {diurnal.map((b, i) => (
            <div key={i} className="flex flex-1 flex-col items-center">
              <div
                className="w-full rounded-t bg-accent/60"
                style={{ height: `${(b.multiplier / peak) * 64 + 4}px` }}
                title={`${b.from_hour}:00–${b.to_hour}:00 ×${b.multiplier}`}
              />
            </div>
          ))}
        </div>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {diurnal.map((b, i) => (
            <label key={i} className="flex items-center justify-between gap-2 text-sm text-text">
              <span className="font-mono text-xs text-text-muted">
                {String(b.from_hour).padStart(2, '0')}:00–{String(b.to_hour).padStart(2, '0')}:00
              </span>
              <Input
                type="number"
                min={0}
                max={INTENSITY_MAX}
                step={0.1}
                value={b.multiplier}
                onChange={(e) => onDiurnalChange(i, clampMult(Number.parseFloat(e.target.value)))}
                className="w-20"
                aria-label={`Multiplier for hours ${b.from_hour} to ${b.to_hour}`}
              />
            </label>
          ))}
        </div>
      </div>

      <div>
        <h4 className="mb-2 text-sm font-medium text-text">Weekly</h4>
        <div className="grid grid-cols-7 gap-2">
          {DAY_ORDER.map((day) => (
            <label key={day} className="flex flex-col items-center gap-1 text-xs text-text-muted">
              <span className="uppercase">{day}</span>
              <Input
                type="number"
                min={0}
                max={INTENSITY_MAX}
                step={0.1}
                value={weekly[day] ?? 1}
                onChange={(e) => onWeeklyChange(day, clampMult(Number.parseFloat(e.target.value)))}
                className="w-full"
                aria-label={`${day} multiplier`}
              />
            </label>
          ))}
        </div>
      </div>

      <p className="text-xs text-text-muted">
        Average TPS is unchanged by curve shape — the engine renormalizes the curve to mean 1.0
        (PRD §4.3).
      </p>
    </div>
  );
}
