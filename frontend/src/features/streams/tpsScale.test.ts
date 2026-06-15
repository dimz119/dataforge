import { describe, expect, it } from 'vitest';

import { clampTps, positionToTps, tpsToPosition, TPS_MAX, TPS_MIN } from './tpsScale';

describe('tpsScale (log-scale slider mapping, §9.5)', () => {
  it('round-trips the endpoints', () => {
    expect(positionToTps(tpsToPosition(TPS_MIN))).toBe(TPS_MIN);
    expect(positionToTps(tpsToPosition(TPS_MAX))).toBe(TPS_MAX);
  });

  it('is monotonic across positions', () => {
    let prev = 0;
    for (let p = 0; p <= 1; p += 0.1) {
      const tps = positionToTps(p);
      expect(tps).toBeGreaterThanOrEqual(prev);
      prev = tps;
    }
  });

  it('is log-scale: the midpoint position maps near the geometric mean, not 500', () => {
    const mid = positionToTps(0.5);
    // sqrt(1 * 1000) ≈ 31.6 — well below the linear midpoint of ~500.
    expect(mid).toBeGreaterThan(20);
    expect(mid).toBeLessThan(50);
  });

  it('hard-clamps to the plan cap', () => {
    expect(positionToTps(1, 100)).toBe(100);
    expect(clampTps(999, 100)).toBe(100);
    expect(clampTps(0, 100)).toBe(TPS_MIN);
    expect(clampTps(50, 100)).toBe(50);
  });

  it('clamps non-finite input to the floor', () => {
    expect(clampTps(Number.NaN)).toBe(TPS_MIN);
  });
});
