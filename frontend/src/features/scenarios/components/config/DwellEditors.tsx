import { FormField, Input } from '../../../../shared/ui';
import type { DwellSpec, TransitionOverride } from '../../overlay';

export interface DwellEditorsProps {
  /** Transitions that declare a dwell family in the manifest. */
  overrides: TransitionOverride[];
  /** Current overlay dwell specs keyed "machine.state.to". */
  values: Record<string, DwellSpec>;
  onChange: (key: string, spec: DwellSpec) => void;
}

/** The editable numeric/duration params per distribution family (the family is fixed). */
const FAMILY_PARAMS: Record<string, string[]> = {
  lognormal: ['median', 'p95'],
  normal: ['mean', 'stddev'],
  fixed: ['value'],
  uniform: ['min', 'max'],
  exponential: ['mean'],
};

/**
 * Dwell distribution editors (frontend-architecture §9.4 DwellEditors). The
 * distribution FAMILY is fixed by the manifest (shown as a read-only label);
 * parameters (median/p95 etc.) are edited as ISO-8601 duration strings (PT2M),
 * bounded by B-15 (≤ P365D) on the server. Only transitions declaring a dwell appear.
 */
export function DwellEditors({ overrides, values, onChange }: DwellEditorsProps) {
  const withDwell = overrides.filter((o) => o.dwellFamily != null);
  if (withDwell.length === 0) {
    return <p className="text-sm text-text-muted">No tunable dwell distributions in this scenario.</p>;
  }
  return (
    <div className="space-y-5">
      {withDwell.map((o) => {
        const family = o.dwellFamily ?? 'fixed';
        const params = FAMILY_PARAMS[family] ?? ['value'];
        const spec = values[o.key] ?? { family };
        return (
          <fieldset key={o.key} className="rounded-md border border-border p-4">
            <legend className="px-1 text-sm font-medium text-text">
              <span className="font-mono text-text-muted">{o.state}</span> → {o.to}
              <span className="ml-2 rounded bg-surface-muted px-1.5 py-0.5 text-[10px] uppercase text-text-muted">
                {family}
              </span>
            </legend>
            <div className="grid grid-cols-2 gap-4">
              {params.map((param) => (
                <FormField key={param} label={param} hint="ISO-8601 duration, e.g. PT2M">
                  {(p) => (
                    <Input
                      value={String(spec[param] ?? '')}
                      placeholder="PT0S"
                      onChange={(e) =>
                        onChange(o.key, { ...spec, family, [param]: e.target.value })
                      }
                      {...p}
                    />
                  )}
                </FormField>
              ))}
            </div>
          </fieldset>
        );
      })}
    </div>
  );
}
