import { describe, expect, it } from 'vitest';

import { formatTps } from './formatTps';

describe('formatTps', () => {
  it('keeps one decimal below 10', () => {
    expect(formatTps(0.55)).toBe('0.6 TPS');
  });

  it('rounds and groups thousands at 10 and above', () => {
    expect(formatTps(1234.4)).toBe('1,234 TPS');
  });

  it('renders a dash for invalid rates', () => {
    expect(formatTps(Number.NaN)).toBe('— TPS');
    expect(formatTps(-1)).toBe('— TPS');
  });
});
