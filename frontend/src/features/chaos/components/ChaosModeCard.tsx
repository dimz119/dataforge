import * as Slider from '@radix-ui/react-slider';
import * as Switch from '@radix-ui/react-switch';
import { useId } from 'react';

import { Input } from '../../../shared/ui';
import { MODE_META, RATE_MAX, type ChaosMode, type ChaosModeConfig } from '../types';

export interface ChaosModeCardProps {
  mode: ChaosMode;
  config: ChaosModeConfig;
  onChange: (next: ChaosModeConfig) => void;
  /** schema_drift can't productively arm until a next schema version exists (INV-REG-5). */
  disabled?: boolean;
  disabledNote?: string;
  /** A `validation-error` detail for this mode (CH-V*), shown under the card. */
  error?: string;
}

const RATE_STEP = 0.01;

/** Read a string param (ISO-8601 duration or window) off the free-form params. */
function paramStr(config: ChaosModeConfig, key: string): string {
  const v = config.params?.[key];
  return typeof v === 'string' ? v : '';
}

/**
 * One chaos mode card (frontend-architecture §9.5 ChaosModeCard): enable Switch, a
 * rate slider hard-capped at 0.5 (B-16 / CH-V01), and the mode-specific param inputs.
 * Durations are simulated time (event-model §3.4 — realized in wall time ÷ speed).
 */
export function ChaosModeCard({
  mode,
  config,
  onChange,
  disabled = false,
  disabledNote,
  error,
}: ChaosModeCardProps) {
  const meta = MODE_META[mode];
  const rateId = useId();
  const set = (patch: Partial<ChaosModeConfig>) => onChange({ ...config, ...patch });
  const setParam = (key: string, value: unknown) =>
    onChange({ ...config, params: { ...config.params, [key]: value } });

  return (
    <div
      className={`space-y-3 rounded-lg border border-border bg-surface p-4 ${disabled ? 'opacity-60' : ''}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-text">{meta.label}</h3>
          <p className="mt-0.5 text-xs text-text-muted">{meta.blurb}</p>
        </div>
        <Switch.Root
          checked={config.enabled}
          disabled={disabled}
          onCheckedChange={(enabled) => set({ enabled })}
          aria-label={`Enable ${meta.label}`}
          className="relative h-5 w-9 shrink-0 rounded-full bg-surface-muted transition-colors data-[state=checked]:bg-accent disabled:opacity-50"
        >
          <Switch.Thumb className="block h-4 w-4 translate-x-0.5 rounded-full bg-surface shadow transition-transform data-[state=checked]:translate-x-[18px]" />
        </Switch.Root>
      </div>

      {config.enabled && !disabled && (
        <div className="space-y-3 border-t border-border pt-3">
          <div className="space-y-1.5">
            <div className="flex items-baseline justify-between">
              <label htmlFor={rateId} className="text-xs font-medium text-text">
                Rate
              </label>
              <span className="font-mono text-xs text-text" aria-live="polite">
                {config.rate.toFixed(2)}
              </span>
            </div>
            <Slider.Root
              id={rateId}
              className="relative flex h-4 w-full touch-none select-none items-center"
              min={RATE_STEP}
              max={RATE_MAX}
              step={RATE_STEP}
              value={[Math.min(config.rate, RATE_MAX)]}
              onValueChange={([r]) => set({ rate: Math.min(r ?? RATE_STEP, RATE_MAX) })}
            >
              <Slider.Track className="relative h-1 w-full grow rounded-full bg-surface-muted">
                <Slider.Range className="absolute h-full rounded-full bg-accent" />
              </Slider.Track>
              <Slider.Thumb
                aria-label={`${meta.label} rate`}
                aria-valuetext={`${config.rate.toFixed(2)}`}
                className="block h-3.5 w-3.5 rounded-full border-2 border-accent bg-surface shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              />
            </Slider.Root>
            <p className="text-[11px] text-text-muted">Max 0.50 (B-16)</p>
          </div>

          {mode === 'late_arriving' && (
            <ParamField label="Median delay (simulated)" hint="realized in wall time ÷ speed">
              <Input
                value={
                  typeof (config.params?.delay as { median?: string })?.median === 'string'
                    ? (config.params?.delay as { median: string }).median
                    : ''
                }
                placeholder="PT30M"
                onChange={(e) =>
                  setParam('delay', {
                    ...(config.params?.delay as object),
                    family: 'lognormal',
                    median: e.target.value,
                  })
                }
              />
            </ParamField>
          )}
          {mode === 'out_of_order' && (
            <ParamField label="Reorder window (simulated)" hint="PT1S – PT5M">
              <Input
                value={paramStr(config, 'window')}
                placeholder="PT60S"
                onChange={(e) => setParam('window', e.target.value)}
              />
            </ParamField>
          )}
          {(mode === 'corrupted_values' || mode === 'nulls') && (
            <ParamField label="Max fields per event" hint="1 – 4">
              <Input
                type="number"
                min={1}
                max={4}
                value={
                  typeof config.params?.max_fields_per_event === 'number'
                    ? config.params.max_fields_per_event
                    : 1
                }
                onChange={(e) => setParam('max_fields_per_event', Number(e.target.value))}
              />
            </ParamField>
          )}
        </div>
      )}

      {disabled && disabledNote && (
        <p className="border-t border-border pt-3 text-xs text-text-muted" role="note">
          {disabledNote}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

function ParamField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-text">{label}</label>
      {children}
      {hint && <p className="text-[11px] text-text-muted">{hint}</p>}
    </div>
  );
}
