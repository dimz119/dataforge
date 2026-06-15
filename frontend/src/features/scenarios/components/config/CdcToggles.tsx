import * as Switch from '@radix-ui/react-switch';

import type { OverlayErrorMap } from '../../overlayErrors';

export interface CdcEntityOption {
  entity: string;
  enabledDefault: boolean;
}

export interface CdcTogglesProps {
  /** CDC-eligible entities from the manifest `cdc.entities` (R-CDC-M1). */
  entities: CdcEntityOption[];
  /** The current enabled set (overlay `cdc_entities`). */
  enabled: Set<string>;
  onToggle: (entity: string, on: boolean) => void;
  errors: OverlayErrorMap;
}

/**
 * CDC entity toggles (frontend-architecture §9.4 CdcToggles). Switches for the
 * manifest-listed `cdc.entities` ONLY (R-CDC-M1: the instance set must be a subset).
 * Errors keyed `cdc:{entity}` or `cdc:*` highlight the group.
 */
export function CdcToggles({ entities, enabled, onToggle, errors }: CdcTogglesProps) {
  const groupErrors = errors['cdc:*'] ?? [];
  if (entities.length === 0) {
    return <p className="text-sm text-text-muted">This scenario declares no CDC-enabled entities.</p>;
  }
  return (
    <div className="space-y-3">
      {groupErrors.length > 0 && (
        <ul role="alert" className="space-y-1">
          {groupErrors.map((e, i) => (
            <li key={i} className="text-xs text-danger">
              {e.message}
            </li>
          ))}
        </ul>
      )}
      <ul className="divide-y divide-border rounded-md border border-border">
        {entities.map((opt) => {
          const on = enabled.has(opt.entity);
          const entityErrors = errors[`cdc:${opt.entity}`] ?? [];
          const id = `cdc-${opt.entity}`;
          return (
            <li key={opt.entity} className="flex items-center justify-between px-4 py-3">
              <label htmlFor={id} className="text-sm text-text">
                {opt.entity}
                {opt.enabledDefault && (
                  <span className="ml-2 text-[10px] uppercase text-text-muted">default on</span>
                )}
                {entityErrors[0] && (
                  <span role="alert" className="ml-2 text-xs text-danger">
                    {entityErrors[0].message}
                  </span>
                )}
              </label>
              <Switch.Root
                id={id}
                checked={on}
                onCheckedChange={(checked) => onToggle(opt.entity, checked)}
                className="relative h-5 w-9 rounded-full bg-surface-muted transition-colors data-[state=checked]:bg-accent"
              >
                <Switch.Thumb className="block h-4 w-4 translate-x-0.5 rounded-full bg-surface shadow transition-transform data-[state=checked]:translate-x-[18px]" />
              </Switch.Root>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
