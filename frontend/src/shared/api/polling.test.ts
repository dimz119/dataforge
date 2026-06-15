import { describe, expect, it } from 'vitest';

import {
  isSettled,
  isTransitional,
  POLL_CONVERGENCE_MS,
  POLL_RUNNING_MS,
  streamDetailInterval,
} from './polling';

describe('streamDetailInterval (§4.4)', () => {
  it('polls every 2 s while a lifecycle command is converging', () => {
    for (const s of ['starting', 'pausing', 'resuming', 'stopping']) {
      expect(streamDetailInterval(s)).toBe(POLL_CONVERGENCE_MS);
    }
  });

  it('polls slowly (10 s) while running or paused', () => {
    expect(streamDetailInterval('running')).toBe(POLL_RUNNING_MS);
    expect(streamDetailInterval('paused')).toBe(POLL_RUNNING_MS);
    expect(streamDetailInterval('paused_quota')).toBe(POLL_RUNNING_MS);
  });

  it('stops polling entirely once settled', () => {
    for (const s of ['stopped', 'failed', 'created']) {
      expect(streamDetailInterval(s)).toBe(false);
    }
  });

  it('treats an unknown/undefined status as transitional (poll to discover state)', () => {
    expect(streamDetailInterval(undefined)).toBe(POLL_CONVERGENCE_MS);
  });
});

describe('isTransitional / isSettled', () => {
  it('classifies transitional statuses', () => {
    expect(isTransitional('starting')).toBe(true);
    expect(isTransitional('running')).toBe(false);
    expect(isTransitional(undefined)).toBe(false);
  });

  it('classifies settled statuses', () => {
    expect(isSettled('stopped')).toBe(true);
    expect(isSettled('running')).toBe(false);
    expect(isSettled(undefined)).toBe(false);
  });
});
