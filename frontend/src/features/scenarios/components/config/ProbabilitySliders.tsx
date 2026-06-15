import * as Slider from '@radix-ui/react-slider';
import { useMemo } from 'react';

import type { TransitionOverride } from '../../overlay';
import type { OverlayError, OverlayErrorMap } from '../../overlayErrors';

export interface ProbabilitySlidersProps {
  overrides: TransitionOverride[];
  /** Current overlay probabilities keyed "machine.state.to" (falls back to default). */
  values: Record<string, number>;
  onChange: (key: string, value: number) => void;
  errors: OverlayErrorMap;
}

const STEP = 0.01;

/** Group transitions by `machine → state` so sum errors highlight a whole group. */
function groupByState(overrides: TransitionOverride[]): Map<string, TransitionOverride[]> {
  const groups = new Map<string, TransitionOverride[]>();
  for (const o of overrides) {
    const id = `${o.machine}.${o.state}`;
    const list = groups.get(id) ?? [];
    list.push(o);
    groups.set(id, list);
  }
  return groups;
}

function GroupErrors({ errors }: { errors: OverlayError[] }) {
  if (errors.length === 0) return null;
  return (
    <ul role="alert" className="mb-2 space-y-1">
      {errors.map((e, i) => (
        <li key={i} className="text-xs text-danger">
          {e.code ? <span className="font-mono">{e.code}: </span> : null}
          {e.message}
        </li>
      ))}
    </ul>
  );
}

/**
 * One slider per overridable transition (frontend-architecture §9.4 ProbabilitySliders),
 * grouped by machine → state. Range is hard-clamped to `[override.min, override.max]`;
 * the manifest default is marked on the track; non-overridable transitions render
 * read-only. MAN-V201 sum errors highlight the offending state's slider GROUP via the
 * OverlayErrorMap (keyed `state:{machine}.{state}`).
 */
export function ProbabilitySliders({
  overrides,
  values,
  onChange,
  errors,
}: ProbabilitySlidersProps) {
  const groups = useMemo(() => groupByState(overrides), [overrides]);

  return (
    <div className="space-y-6">
      {[...groups.entries()].map(([groupId, transitions]) => {
        const [machine, state] = groupId.split('.', 2);
        const groupErrors = errors[`state:${groupId}`] ?? [];
        const invalid = groupErrors.length > 0;
        return (
          <fieldset
            key={groupId}
            className={`rounded-md border p-4 ${invalid ? 'border-danger' : 'border-border'}`}
          >
            <legend className="px-1 text-sm font-medium text-text">
              <span className="font-mono text-text-muted">{machine}</span> · {state}
            </legend>
            <GroupErrors errors={groupErrors} />
            <div className="space-y-4">
              {transitions.map((t) => {
                const current = values[t.key] ?? t.default;
                const min = t.allowed ? t.min : t.default;
                const max = t.allowed ? t.max : t.default;
                return (
                  <div key={t.key}>
                    <div className="flex items-baseline justify-between">
                      <label
                        htmlFor={`prob-${t.key}`}
                        className="text-sm text-text"
                      >
                        → {t.to}
                        {!t.allowed && (
                          <span className="ml-2 text-[10px] uppercase text-text-muted">locked</span>
                        )}
                      </label>
                      <span className="font-mono text-sm text-text" aria-live="polite">
                        {current.toFixed(2)}
                      </span>
                    </div>
                    <Slider.Root
                      id={`prob-${t.key}`}
                      className="relative mt-1.5 flex h-5 w-full touch-none select-none items-center"
                      min={min}
                      max={max}
                      step={STEP}
                      value={[Math.max(min, Math.min(max, current))]}
                      disabled={!t.allowed}
                      onValueChange={([v]) => onChange(t.key, v ?? current)}
                    >
                      <Slider.Track className="relative h-1.5 w-full grow rounded-full bg-surface-muted">
                        <Slider.Range className="absolute h-full rounded-full bg-accent" />
                      </Slider.Track>
                      <Slider.Thumb
                        aria-label={`${state} → ${t.to} probability`}
                        aria-valuetext={current.toFixed(2)}
                        aria-disabled={!t.allowed || undefined}
                        className="block h-4 w-4 rounded-full border-2 border-accent bg-surface shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                      />
                    </Slider.Root>
                    <div className="mt-0.5 flex justify-between text-[10px] text-text-muted">
                      <span>{min.toFixed(2)}</span>
                      <span>default {t.default.toFixed(2)}</span>
                      <span>{max.toFixed(2)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </fieldset>
        );
      })}
    </div>
  );
}
