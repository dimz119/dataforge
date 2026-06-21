import { describe, expect, it } from 'vitest';

import { CHAOS_PRESETS, presetToDocument } from './presets';
import { CHAOS_MODES } from './types';

describe('chaos exercise presets (chaos-engine §8 / PRD §5)', () => {
  it('ships the five Phase-9 exercise presets', () => {
    expect(CHAOS_PRESETS.map((p) => p.slug)).toEqual([
      'dedup_101',
      'late_data_30min',
      'out_of_order_60s',
      'drift_day',
      'dlq_day',
    ]);
  });

  it('expands a preset to a full seven-mode document, disabling unlisted modes', () => {
    const dedup = CHAOS_PRESETS.find((p) => p.slug === 'dedup_101')!;
    const doc = presetToDocument(dedup, 'discard');

    // All seven keys present (closed shape, CH-V09).
    for (const mode of CHAOS_MODES) expect(doc[mode]).toBeDefined();
    expect(doc.duplicates.enabled).toBe(true);
    expect(doc.duplicates.rate).toBeCloseTo(0.05);
    // Unlisted modes are disabled (the "replaces the whole document" rule, §8).
    expect(doc.late_arriving.enabled).toBe(false);
    expect(doc.on_stop_policy).toBe('discard');
  });

  it('keeps every preset rate within the 0.5 cap (B-16)', () => {
    for (const preset of CHAOS_PRESETS) {
      for (const cfg of Object.values(preset.modes)) {
        expect(cfg.rate).toBeGreaterThan(0);
        expect(cfg.rate).toBeLessThanOrEqual(0.5);
      }
    }
  });
});
