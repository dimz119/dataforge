import { useState } from 'react';

import { Button, Input } from '../../../shared/ui';

export interface EntityFilterValue {
  entityType: string;
  entityKey: string;
}

export interface EntityFilterProps {
  /** The applied filter, or null when none is active. */
  value: EntityFilterValue | null;
  /** Apply (both fields set) or clear (null) the per-entity CDC filter. */
  onChange: (value: EntityFilterValue | null) => void;
}

/**
 * The per-entity CDC filter for the live tail (event-model R-CDC-7; Phase 8
 * frontend-architecture §13). `entity_type` + `entity_key` (both or neither) are sent
 * in the WS auth frame and matched against `entity_refs` — IDENTICAL semantics to the
 * REST `?entity_type=&entity_key=` params. Changing it recreates the socket (WS-5) and
 * the REST gap-fill resends the same filter so the bound cursor still decodes (RC-7).
 */
export function EntityFilter({ value, onChange }: EntityFilterProps) {
  const [type, setType] = useState(value?.entityType ?? '');
  const [key, setKey] = useState(value?.entityKey ?? '');
  const canApply = type.trim() !== '' && key.trim() !== '';

  const apply = () => {
    if (canApply) onChange({ entityType: type.trim(), entityKey: key.trim() });
  };
  const clear = () => {
    setType('');
    setKey('');
    onChange(null);
  };

  return (
    <div className="flex items-center gap-1.5" aria-label="Per-entity CDC filter">
      <Input
        value={type}
        onChange={(e) => setType(e.target.value)}
        placeholder="entity_type"
        aria-label="Entity type"
        className="h-7 w-28 text-xs"
        onKeyDown={(e) => e.key === 'Enter' && apply()}
      />
      <Input
        value={key}
        onChange={(e) => setKey(e.target.value)}
        placeholder="entity_key"
        aria-label="Entity key"
        className="h-7 w-36 text-xs"
        onKeyDown={(e) => e.key === 'Enter' && apply()}
      />
      <Button size="sm" variant="secondary" onClick={apply} disabled={!canApply}>
        Filter entity
      </Button>
      {value && (
        <Button size="sm" variant="ghost" onClick={clear} aria-label="Clear entity filter">
          ✕
        </Button>
      )}
    </div>
  );
}
