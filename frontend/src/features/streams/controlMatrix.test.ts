import { describe, expect, it } from 'vitest';

import {
  CONTROL_MATRIX,
  controlRow,
  startLabel,
  type StreamStatus,
} from './controlMatrix';

/**
 * The normative §9.5 button-enablement matrix. These assertions ARE the spec table —
 * a failure here means a behavior change. Cells: enabled / pending / disabled / hidden.
 */
describe('CONTROL_MATRIX (frontend-architecture §9.5, normative)', () => {
  // [status, start, pause, resume, stop, tps, delete] verbatim from the spec table.
  const TABLE: [StreamStatus, ...string[]][] = [
    ['created', 'enabled', 'hidden', 'hidden', 'hidden', 'hidden', 'enabled'],
    ['starting', 'pending', 'hidden', 'hidden', 'enabled', 'hidden', 'hidden'],
    ['running', 'hidden', 'enabled', 'hidden', 'enabled', 'enabled', 'hidden'],
    ['pausing', 'hidden', 'pending', 'hidden', 'enabled', 'hidden', 'hidden'],
    ['paused', 'hidden', 'hidden', 'enabled', 'enabled', 'hidden', 'hidden'],
    ['paused_quota', 'hidden', 'hidden', 'disabled', 'enabled', 'hidden', 'hidden'],
    ['paused_idle', 'hidden', 'hidden', 'enabled', 'enabled', 'hidden', 'hidden'],
    ['resuming', 'hidden', 'hidden', 'pending', 'enabled', 'hidden', 'hidden'],
    ['stopping', 'hidden', 'hidden', 'hidden', 'pending', 'hidden', 'hidden'],
    ['stopped', 'enabled', 'hidden', 'hidden', 'hidden', 'hidden', 'enabled'],
    ['failed', 'enabled', 'hidden', 'hidden', 'hidden', 'hidden', 'enabled'],
  ];

  it.each(TABLE)(
    '%s → start=%s pause=%s resume=%s stop=%s tps=%s delete=%s',
    (status, start, pause, resume, stop, tps, del) => {
      const row = CONTROL_MATRIX[status];
      expect(row.start).toBe(start);
      expect(row.pause).toBe(pause);
      expect(row.resume).toBe(resume);
      expect(row.stop).toBe(stop);
      expect(row.tps).toBe(tps);
      expect(row.delete).toBe(del);
    },
  );

  it('enables the TPS slider ONLY while running', () => {
    for (const [status, row] of Object.entries(CONTROL_MATRIX)) {
      expect(row.tps === 'enabled').toBe(status === 'running');
    }
  });

  it('disables (not hides) resume only in paused_quota — the T7 headroom guard', () => {
    expect(CONTROL_MATRIX.paused_quota.resume).toBe('disabled');
    expect(CONTROL_MATRIX.paused.resume).toBe('enabled');
    expect(CONTROL_MATRIX.paused_idle.resume).toBe('enabled');
  });

  it('allows delete only from created/stopped/failed (T14)', () => {
    const deletable = Object.entries(CONTROL_MATRIX)
      .filter(([, row]) => row.delete === 'enabled')
      .map(([s]) => s)
      .sort();
    expect(deletable).toEqual(['created', 'failed', 'stopped']);
  });

  it('falls back to an all-hidden row for an unknown status', () => {
    const row = controlRow('garbage');
    expect(Object.values(row).every((s) => s === 'hidden')).toBe(true);
  });

  it('labels the start verb as Retry from failed (T13), Start otherwise (T12)', () => {
    expect(startLabel('failed')).toBe('Retry');
    expect(startLabel('stopped')).toBe('Start');
    expect(startLabel('created')).toBe('Start');
  });
});
