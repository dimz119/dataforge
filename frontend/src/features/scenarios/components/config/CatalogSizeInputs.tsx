import { useMemo } from 'react';

import { FormField, Input } from '../../../../shared/ui';
import { CATALOG_SUM_CAP, type CatalogBound } from '../../overlay';
import type { OverlayErrorMap } from '../../overlayErrors';

export interface CatalogSizeInputsProps {
  bounds: CatalogBound[];
  /** Current overlay catalog_sizes keyed by entity (falls back to manifest default). */
  values: Record<string, number>;
  onChange: (entity: string, value: number) => void;
  errors: OverlayErrorMap;
}

/**
 * Per-entity catalog-size inputs (frontend-architecture §9.4 CatalogSizeInputs).
 * Each input is clamped to the manifest `[min, max]`; a live Σ indicator shows the
 * total against the 250,000 cap (B-08). Per-entity MAN-V errors surface via the
 * OverlayErrorMap keyed `catalog:{entity}`.
 */
export function CatalogSizeInputs({ bounds, values, onChange, errors }: CatalogSizeInputsProps) {
  const total = useMemo(
    () => bounds.reduce((sum, b) => sum + (values[b.entity] ?? b.default), 0),
    [bounds, values],
  );
  const overCap = total > CATALOG_SUM_CAP;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {bounds.map((b) => {
          const current = values[b.entity] ?? b.default;
          const entityErrors = errors[`catalog:${b.entity}`] ?? [];
          const error = entityErrors[0]?.message;
          return (
            <FormField
              key={b.entity}
              label={b.entity}
              error={error}
              hint={`${b.min.toLocaleString('en-US')}–${b.max.toLocaleString('en-US')}`}
            >
              {(p) => (
                <Input
                  type="number"
                  min={b.min}
                  max={b.max}
                  value={current}
                  onChange={(e) => {
                    const raw = Number.parseInt(e.target.value, 10);
                    const clamped = Number.isNaN(raw)
                      ? b.min
                      : Math.max(b.min, Math.min(b.max, raw));
                    onChange(b.entity, clamped);
                  }}
                  {...p}
                />
              )}
            </FormField>
          );
        })}
      </div>
      <p
        className={`text-sm ${overCap ? 'text-danger' : 'text-text-muted'}`}
        aria-live="polite"
        role={overCap ? 'alert' : undefined}
      >
        Σ {total.toLocaleString('en-US')} / {CATALOG_SUM_CAP.toLocaleString('en-US')} cap (B-08)
        {overCap && ' — over the cap; reduce catalog sizes before saving.'}
      </p>
    </div>
  );
}
