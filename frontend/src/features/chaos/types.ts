/**
 * Strong client-side types for the ChaosPolicy document (chaos-engine §3.2). The
 * OpenAPI contract types the wire shape loosely (`modes: additionalProperties: {}`,
 * chaos-engine §3.5 mode-level merge), so the seven-mode closed shape (CH-V09) lives
 * here. Mode identifiers are the exact seven `ChaosMode` strings (domain model §2.7).
 */

/** The seven canonical chaos modes, in panel display order (chaos-engine §3.2). */
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

/** `on_stop_policy ∈ {discard, flush}`, default `discard` (chaos-engine §6.3). */
export type OnStopPolicy = 'discard' | 'flush';

/** One mode entry: enable + rate (0 < rate ≤ 0.5, B-16) + free-form params (§3.2). */
export interface ChaosModeConfig {
  enabled: boolean;
  rate: number;
  params?: Record<string, unknown>;
}

/** The full per-stream document: the seven mode keys plus `on_stop_policy`. */
export type ChaosPolicyDocument = Record<ChaosMode, ChaosModeConfig> & {
  on_stop_policy: OnStopPolicy;
};

/** Hard upper bound on every mode rate (B-16 / CH-V01: `0 < rate ≤ 0.5`). */
export const RATE_MAX = 0.5;

/** Human-facing mode labels + the one-line description shown on each card. */
export const MODE_META: Record<ChaosMode, { label: string; blurb: string }> = {
  duplicates: { label: 'Duplicates', blurb: 'Re-deliver eligible events one or more times.' },
  late_arriving: {
    label: 'Late arriving',
    blurb: 'Hold and re-emit events after a simulated delay.',
  },
  missing: { label: 'Missing', blurb: 'Suppress delivery of eligible events.' },
  out_of_order: {
    label: 'Out of order',
    blurb: 'Displace events within a reorder window.',
  },
  corrupted_values: {
    label: 'Corrupted values',
    blurb: 'Mutate scalar leaf fields in delivered payloads.',
  },
  nulls: { label: 'Nulls', blurb: 'Null out eligible scalar leaf fields.' },
  schema_drift: {
    label: 'Schema drift',
    blurb: 'Inject next-version fields into delivered payloads.',
  },
};

/** Sensible zero-rate default for a disabled mode toggled on without a preset value. */
export function defaultMode(): ChaosModeConfig {
  return { enabled: false, rate: 0.05, params: {} };
}
