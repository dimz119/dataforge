import { useId } from 'react';

import type { OnStopPolicy } from '../types';

export interface OnStopPolicySelectProps {
  value: OnStopPolicy;
  onChange: (next: OnStopPolicy) => void;
}

/**
 * OnStopPolicySelect (frontend-architecture §9.5; domain model §2.7 OnStopPolicy).
 * Governs pending late re-emissions at stop (chaos-engine §6.3): `discard` (default)
 * drops them; `flush` publishes them immediately before the lease releases.
 */
export function OnStopPolicySelect({ value, onChange }: OnStopPolicySelectProps) {
  const id = useId();
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="text-sm font-medium text-text">
        On stop: pending late re-emissions
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value as OnStopPolicy)}
        className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text focus:outline-none"
      >
        <option value="discard">Discard (default) — drop pending late events</option>
        <option value="flush">Flush — publish pending late events before stopping</option>
      </select>
    </div>
  );
}
