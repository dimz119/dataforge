import * as Switch from '@radix-ui/react-switch';

import { Input } from '../../../../shared/ui';
import type { OverlayErrorMap } from '../../overlayErrors';

/** The 7 canonical chaos modes (domain model §2.7 — exact identifiers). */
export const CHAOS_MODES = [
  'duplicates',
  'late_arriving',
  'missing',
  'out_of_order',
  'corrupted_values',
  'nulls',
  'schema_drift',
] as const;

export type ChaosMode = (typeof CHAOS_MODES)[number];

/** Per-mode default config we write into the overlay. */
export interface ChaosModeDefault {
  enabled?: boolean;
  rate?: number;
}

/** Rate cap per mode (B-16). */
export const CHAOS_RATE_MAX = 0.5;

export interface ChaosDefaultsSectionProps {
  values: Record<string, ChaosModeDefault>;
  onChange: (mode: ChaosMode, next: ChaosModeDefault) => void;
  errors: OverlayErrorMap;
}

const MODE_LABEL: Record<ChaosMode, string> = {
  duplicates: 'Duplicates',
  late_arriving: 'Late arriving',
  missing: 'Missing',
  out_of_order: 'Out of order',
  corrupted_values: 'Corrupted values',
  nulls: 'Nulls',
  schema_drift: 'Schema drift',
};

/**
 * Chaos DEFAULTS section (frontend-architecture §9.4 ChaosDefaultsSection). These
 * write the INSTANCE chaos defaults (the overlay `chaos` block) — NOT the live,
 * per-stream chaos policy, which is the Phase 9 ChaosPanel (§9.5, live-mutable per
 * PIN-3). One enable Switch + rate input (0–0.5, B-16) per canonical mode. Full
 * per-mode parameter editors (delay distributions, windows) are Phase 9.
 */
export function ChaosDefaultsSection({ values, onChange, errors }: ChaosDefaultsSectionProps) {
  return (
    <div className="space-y-3">
      <p className="text-xs text-text-muted">
        These are the instance&apos;s chaos defaults — new streams inherit them. The live,
        per-stream chaos panel arrives in a later phase.
      </p>
      <ul className="divide-y divide-border rounded-md border border-border">
        {CHAOS_MODES.map((mode) => {
          const cfg = values[mode] ?? {};
          const on = cfg.enabled === true;
          const rate = cfg.rate ?? 0;
          const modeErrors = errors[`chaos:${mode}`] ?? [];
          return (
            <li key={mode} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
              <label htmlFor={`chaos-${mode}`} className="text-sm text-text">
                {MODE_LABEL[mode]}
                {modeErrors[0] && (
                  <span role="alert" className="ml-2 text-xs text-danger">
                    {modeErrors[0].message}
                  </span>
                )}
              </label>
              <div className="flex items-center gap-3">
                <Input
                  type="number"
                  min={0}
                  max={CHAOS_RATE_MAX}
                  step={0.01}
                  value={rate}
                  disabled={!on}
                  onChange={(e) => {
                    const raw = Number.parseFloat(e.target.value);
                    const clamped = Number.isNaN(raw)
                      ? 0
                      : Math.max(0, Math.min(CHAOS_RATE_MAX, raw));
                    onChange(mode, { ...cfg, enabled: on, rate: clamped });
                  }}
                  className="w-24"
                  aria-label={`${MODE_LABEL[mode]} rate`}
                />
                <Switch.Root
                  id={`chaos-${mode}`}
                  checked={on}
                  onCheckedChange={(checked) =>
                    onChange(mode, { ...cfg, enabled: checked, rate })
                  }
                  className="relative h-5 w-9 rounded-full bg-surface-muted transition-colors data-[state=checked]:bg-accent"
                >
                  <Switch.Thumb className="block h-4 w-4 translate-x-0.5 rounded-full bg-surface shadow transition-transform data-[state=checked]:translate-x-[18px]" />
                </Switch.Root>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
