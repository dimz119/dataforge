/**
 * Exercise presets — named, platform-defined ChaosPolicy bundles versioned with the
 * app (chaos-engine §8; PRD §5 catalog). NOT catalog rows: they are constants, so the
 * same preset + same seed reproduces an identical lab next semester (ADR-0008).
 *
 * Applying a preset REPLACES the whole seven-mode document (unlisted modes →
 * `enabled: false`) so labs start from a known-exact state (chaos-engine §8 semantics).
 * A frontend constant is the simplest home (the spec allows a console-local catalog;
 * the API also lists them, but inspection-before-apply works equally from here).
 */
import type { ChaosMode, ChaosModeConfig, ChaosPolicyDocument } from './types';
import { CHAOS_MODES, defaultMode } from './types';

export interface ChaosPreset {
  /** Stable slug (chaos-engine §8 table). */
  slug: string;
  /** Display name shown in the picker (verbatim from chaos-engine §8). */
  name: string;
  /** PRD §5 exercise id this preset implements. */
  exercise: string;
  /** One-line "what it teaches" summary for the picker. */
  description: string;
  /** The enabled modes; the bundle disables every unlisted mode on apply. */
  modes: Partial<Record<ChaosMode, ChaosModeConfig>>;
}

/** The five Phase-9 exercise presets (chaos-engine §8 / PRD §5). */
export const CHAOS_PRESETS: readonly ChaosPreset[] = [
  {
    slug: 'dedup_101',
    name: 'Dedup 101',
    exercise: 'E1',
    description: 'Duplicate ~5% of events so consumers must de-duplicate.',
    modes: {
      duplicates: {
        enabled: true,
        rate: 0.05,
        params: { copies: [{ count: 1, weight: 1.0 }], spacing: { mode: 'adjacent' } },
      },
    },
  },
  {
    slug: 'late_data_30min',
    name: 'Late data 30min',
    exercise: 'E2',
    description: 'Hold ~3% of events ~30 simulated minutes late (lognormal).',
    modes: {
      late_arriving: {
        enabled: true,
        rate: 0.03,
        params: {
          delay: { family: 'lognormal', median: 'PT30M', p95: 'PT2H' },
          max_delay: 'PT24H',
        },
      },
    },
  },
  {
    slug: 'out_of_order_60s',
    name: 'Out-of-order 60s',
    exercise: 'E3',
    description: 'Reorder ~10% of events within a 60-second window.',
    modes: {
      out_of_order: { enabled: true, rate: 0.1, params: { window: 'PT60S' } },
    },
  },
  {
    slug: 'drift_day',
    name: 'Drift day',
    exercise: 'E5',
    description: 'Inject next-version fields into ~20% of payloads (needs a next version).',
    modes: {
      schema_drift: {
        enabled: true,
        rate: 0.2,
        params: { subjects: ['*'], fields: ['*'] },
      },
    },
  },
  {
    slug: 'dlq_day',
    name: 'DLQ day',
    exercise: 'E6',
    description: 'Corrupt ~2% of values and null ~2% of fields for dead-letter handling.',
    modes: {
      corrupted_values: { enabled: true, rate: 0.02, params: { max_fields_per_event: 1 } },
      nulls: { enabled: true, rate: 0.02, params: { max_fields_per_event: 1 } },
    },
  },
];

/**
 * Expand a preset into a full seven-mode document at the given `on_stop_policy`.
 * Modes not named by the preset are emitted disabled at their default rate (the
 * "replaces the whole document" rule, chaos-engine §8).
 */
export function presetToDocument(
  preset: ChaosPreset,
  onStopPolicy: ChaosPolicyDocument['on_stop_policy'],
): ChaosPolicyDocument {
  const doc = { on_stop_policy: onStopPolicy } as ChaosPolicyDocument;
  for (const mode of CHAOS_MODES) {
    doc[mode] = preset.modes[mode] ?? { ...defaultMode() };
  }
  return doc;
}
