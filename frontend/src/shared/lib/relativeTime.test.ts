import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { formatRelativeTime } from './relativeTime';

describe('formatRelativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns "never" for null/empty/invalid', () => {
    expect(formatRelativeTime(null)).toBe('never');
    expect(formatRelativeTime(undefined)).toBe('never');
    expect(formatRelativeTime('not-a-date')).toBe('never');
  });

  it('formats recent past timestamps', () => {
    expect(formatRelativeTime('2026-06-14T11:57:00Z')).toBe('3 minutes ago');
  });

  it('formats hours and days ago', () => {
    expect(formatRelativeTime('2026-06-14T09:00:00Z')).toBe('3 hours ago');
    expect(formatRelativeTime('2026-06-12T12:00:00Z')).toBe('2 days ago');
  });

  it('formats future timestamps', () => {
    expect(formatRelativeTime('2026-06-16T12:00:00Z')).toBe('in 2 days');
  });
});
